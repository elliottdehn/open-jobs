import { ByteWriter, ParquetWriter, type SchemaElement } from "hyparquet-writer";
import type { StoredJob } from "./board";

/**
 * Per-board R2 parquet snapshots ("current openings"). After a board's diff changes AND its embed
 * backlog drains (see Board.maybeSnapshot), the DO writes one parquet file of its OPEN jobs to R2 at
 * snapshots/{ats}/{slug}.parquet — vectors as a real float LIST column (6 KB vs ~30 KB as JSON text),
 * board meta in the footer key-value metadata. Consolidation then bulk-downloads these static files
 * instead of paging JSON out of 100k DOs, and the DO's SQLite remains the source of truth
 * (history + removed jobs live only there).
 *
 * Excluded from snapshots (fetch via /export if ever needed): raw/detailRaw provider payloads —
 * they're debug-only weight, not consumed by the parquet build, manifest, or search.
 */

/** Explicit parquet schema (hyparquet-writer needs it for the LIST<FLOAT> embedding column). */
const SCHEMA = [
	{ name: "root", num_children: 21 },
	...["ats", "slug", "id", "title", "location", "url", "departments_json", "published_at", "updated_at",
		"content", "detail_status", "content_hash", "enrich_status", "enrichment_json", "embed_status",
		"embed_model", "org"].map((name) => ({ name, type: "BYTE_ARRAY", converted_type: "UTF8", repetition_type: "OPTIONAL" })),
	...["first_seen_ms", "last_seen_ms", "changed_ms"].map((name) => ({ name, type: "INT64", repetition_type: "OPTIONAL" })),
	{ name: "embedding", repetition_type: "OPTIONAL", converted_type: "LIST", num_children: 1 },
	{ name: "list", repetition_type: "REPEATED", num_children: 1 },
	{ name: "element", type: "FLOAT", repetition_type: "REQUIRED" },
] as SchemaElement[];

const CHUNK = 400; // rows converted + written per ParquetWriter.write call (bounds peak memory)

export interface SnapshotSource {
	ats: string;
	slug: string;
	/** Iterate OPEN jobs only (removed_at IS NULL); consumed lazily, one chunk in memory at a time. */
	jobs: Iterable<StoredJob & { embeddingBuf?: ArrayBuffer | null }>;
	/** Board meta JSON for the parquet footer (drives boards.parquet downstream). */
	meta: unknown;
}

/** Build the snapshot parquet for one board. Returns the file bytes. */
export function buildSnapshotParquet(src: SnapshotSource): Uint8Array {
	const writer = new ByteWriter();
	const pw = new ParquetWriter({
		writer,
		schema: SCHEMA,
		kvMetadata: [{ key: "board_meta", value: JSON.stringify(src.meta ?? null) }],
	});
	let chunk: (StoredJob & { embeddingBuf?: ArrayBuffer | null })[] = [];
	const flush = () => {
		if (!chunk.length) return;
		const col = (f: (j: (typeof chunk)[number]) => unknown) => chunk.map(f);
		pw.write({
			columnData: [
				{ name: "ats", data: col(() => src.ats) },
				{ name: "slug", data: col(() => src.slug) },
				{ name: "id", data: col((j) => j.id) },
				{ name: "title", data: col((j) => j.title ?? null) },
				{ name: "location", data: col((j) => j.location ?? null) },
				{ name: "url", data: col((j) => j.url ?? null) },
				{ name: "departments_json", data: col((j) => (j.departments?.length ? JSON.stringify(j.departments) : null)) },
				{ name: "published_at", data: col((j) => j.publishedAt ?? null) },
				{ name: "updated_at", data: col((j) => j.updatedAt ?? null) },
				{ name: "content", data: col((j) => j.content ?? null) },
				{ name: "detail_status", data: col((j) => j.detailStatus ?? null) },
				{ name: "content_hash", data: col((j) => j.contentHash ?? null) },
				{ name: "enrich_status", data: col((j) => j.enrichStatus ?? null) },
				{ name: "enrichment_json", data: col((j) => (j.enrichment ? JSON.stringify(j.enrichment) : null)) },
				{ name: "embed_status", data: col((j) => j.embedStatus ?? null) },
				{ name: "embed_model", data: col((j) => j.embedModel ?? null) },
				{ name: "org", data: col((j) => (j as { org?: string | null }).org ?? null) },
				{ name: "first_seen_ms", data: col((j) => (j.firstSeenAt != null ? BigInt(j.firstSeenAt) : null)) },
				{ name: "last_seen_ms", data: col((j) => (j.lastSeenAt != null ? BigInt(j.lastSeenAt) : null)) },
				{ name: "changed_ms", data: col((j) => (j.changedAt != null ? BigInt(j.changedAt) : null)) },
				// dremel encoder requires plain arrays; converted per-chunk so peak heap stays bounded
				{ name: "embedding", data: col((j) => (j.embeddingBuf ? Array.from(new Float32Array(j.embeddingBuf)) : null)) },
			],
			rowGroupSize: CHUNK,
		});
		chunk = [];
	};
	for (const j of src.jobs) {
		chunk.push(j);
		if (chunk.length >= CHUNK) flush();
	}
	flush();
	pw.finish();
	return new Uint8Array(writer.getBuffer());
}

/** R2 object key for a board's snapshot (slug URI-encoded: slugs can contain '/', ':', unicode). */
export function snapshotKey(ats: string, slug: string): string {
	return `snapshots/${ats}/${encodeURIComponent(slug)}.parquet`;
}
