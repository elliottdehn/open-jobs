# Open Jobs Support Form

Google Form copy. Title, description, then each question with its type and help text.

---

**Title:** Open Jobs Support Form

**Description (under the title):**

Open Jobs is free, CC0, and rebuilt every night. This form is for people building something on it who need more than "it is probably there in the morning": a person to email, a guaranteed daily run, help with a mirror, or a change to what gets published. Tell me what you are doing and what you need. I read every answer and reply personally. Nothing here is a commitment on either side.

---

## 1. Email

Type: short answer, required, validated as email.

Help text: Where I should reply.

## 2. What are you building, or trying to do?

Type: paragraph, required.

Help text: A sentence or two is plenty. A job board for one industry, a research project, a mirror in your own datacenter, a feed into a database, an agent that files applications. Whatever it is.

## 3. Which parts of the data do you use?

Type: checkboxes.

- The full nightly export (parquet, 13 GB)
- The search index and group files (what the search page reads)
- The daily diffs
- The ledger (every posting ever recorded, with first-seen and removed dates)
- The change feed (paged, for mirrors)
- The API (`/embed`, `/jd`, `/status`, `/probe`)
- Not using it yet, evaluating

## 4. How often do you need it fresh?

Type: multiple choice.

- Every morning, without gaps
- Most days is fine
- Weekly is fine
- Once, as a snapshot

## 5. What do you need that you do not have today?

Type: checkboxes.

- A person to email when something breaks
- A guaranteed daily run, with someone accountable for it
- Help setting up a mirror or a self-hosted search page
- A field, a source, or a format that is not published yet
- Bulk access to the enrichment or embeddings for my own key
- Something else (say what below)

## 6. Anything else?

Type: paragraph, optional.

Help text: Constraints, deadlines, the ATS you wish were covered, the thing that annoys you.

## 7. Paying for it

Type: checkboxes.

- I would pay for support or a guaranteed daily run
- I could contribute hardware, bandwidth, or a mirror instead of money
- I am a researcher or a nonprofit and would need it to stay free
- Not at this point

## 8. Rough budget, if any

Type: multiple choice, optional.

- Under $50 a month
- $50 to $250 a month
- $250 to $1,000 a month
- More than that
- Not sure yet

---

**Confirmation message (after submit):**

Thanks. I will reply from my own address within a few days. In the meantime, three commands put the whole dataset on your disk: https://backend.dehnbostele.workers.dev/data/

---

## Where it goes

One line at the end of the "Take the data" section of the README, added after the awesome-public-datasets entry is merged:

> Building something on this? Tell me what: https://forms.gle/S1mZXwLZ1ZzrXbv37. If you need it to be there every morning, say so, and I will tell you honestly what I can commit to.
