# Architecture Plan: Post-Run Interactive Interview & Dynamic RAG Learning Loop

## 1. Overview & Vision
When applying to jobs at scale across various ATS platforms, application forms frequently include non-deterministic or company-specific questions (e.g. security clearance, salary expectations, on-site willingness, conflict-of-interest declarations).

Rather than halting batch execution in the middle of a run or silently failing, the agent operates in two coordinated phases:
1. **Uninterrupted Batch Phase**: Unanswerable required questions mark the specific application as `needs_review` and record the structured question metadata (question text, choices/options, field type, company). The batch continues uninterrupted.
2. **Post-Run Interactive Interview Phase**: At the end of the run (or via `python -m agent interview`), the agent surfaces all unanswered questions to the user, gathers the user's answers interactively, persists them into RAG and `profile.yaml`, and offers to immediately re-apply to the unlocked jobs.

---

## 2. Architecture & Workflow

```
+-----------------------------------------------------------------------------------+
| 1. BATCH RUN                                                                      |
|                                                                                   |
|  [Form Fields Extracted] -> [RAG & Profile Resolution]                            |
|                                     |                                             |
|                                     v                                             |
|                         [Unanswered Required Field?]                              |
|                                     |                                             |
|                                     +---> YES: Record in DB `unanswered_json`     |
|                                     |          (label, options, kind, company)    |
|                                     |          Mark `needs_review`, continue batch|
|                                     |                                             |
|                                     +---> NO: Fill & Submit                       |
+-----------------------------------------------------------------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
| 2. POST-RUN INTERACTIVE INTERVIEW (`agent/interview.py`)                          |
|                                                                                   |
|  [Batch Finished] -> Prompt User:                                                 |
|  "Found 4 unanswered questions across 3 jobs. Answer now? [Y/n]"                  |
|                                     |                                             |
|                                     v                                             |
|  Question 1/4 (Company: Alstom):                                                  |
|  "Are you willing to work on-site in Pittsburgh 5 days a week?"                   |
|  Choices: [1] Yes  [2] No                                                         |
|  > User selection: 1                                                              |
+-----------------------------------------------------------------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
| 3. PERSISTENCE & RAG GROUNDING                                                    |
|                                                                                   |
|  * General questions   -> `profile.yaml` under `custom_answers` / RAG vector db   |
|  * Company questions   -> `profile.yaml` under `company_answers.<company>`        |
|  * SQLite Answer Bank  -> `answers_bank` table in `data/apply.db`                 |
|  * Invalidate RAG and resume cache so new answers are immediately searchable      |
+-----------------------------------------------------------------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
| 4. AUTOMATIC RE-APPLICATION                                                       |
|                                                                                   |
|  Prompt User: "Answers saved to RAG. Re-apply to the 3 unlocked jobs now? [Y/n]"  |
|  -> Automatically re-runs `apply` for those jobs with grounded answers.           |
+-----------------------------------------------------------------------------------+
```

---

## 3. Implementation Details

### Phase 1: Rich Unanswered Serialization
- **`agent/form.py` & `agent/ats/base.py`**:
  Update `unanswered_required` in `ApplyResult` from plain strings to structured dictionaries:
  ```python
  {
      "key": field.key,
      "label": field.label or field.key,
      "kind": field.kind,
      "options": field.options,
      "company": job.company,
  }
  ```
- **`agent/db.py`**:
  Store this structured list into `unanswered_json` in the SQLite `applications` table.

### Phase 2: Interactive Interview Module (`agent/interview.py`)
- CLI interactive loop with numbered options for select/radio fields.
- Deduplication of identical questions across different companies.
- Categorization into general vs. company-specific answers.

### Phase 3: Dynamic Profile & RAG Ingestion
- Clean atomic updates to `profile.yaml` preserving formatting.
- Insertion into `answers_bank` table.
- Cache clearance of RAG text/embedding caches.

### Phase 4: CLI Command & Runner Integration
- Add `agent interview` CLI command to `agent/__main__.py`.
- Hook into the end of `agent apply` when running in an interactive terminal.
- Offer single-keypress re-application on newly resolved jobs.
