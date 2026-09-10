Fair point, and done. One link now downloads the whole dataset:

https://backend.dehnbostele.workers.dev/data/exports/open-jobs-latest.tar

It is a plain tar of parquet files, about 13 GB, one file per applicant tracking system plus one per career site's metadata and a README. Rebuilt every night at that same URL, and the server supports HTTP Range, so a browser or curl -C - can resume it. No account, no key.

I updated the entry: that link is now the first source, and the description says so. The index page at https://backend.dehnbostele.workers.dev/data/ has the same button at the top, with the three-command route kept underneath for people who want a query shell over the files.
