# ADR-0028 — The SmartStore tag stage: the platform tag endpoints, the SearchSignal port and `recommended_tags`

Status: **ACCEPTED** 2026-10-09. This is the contract for the next items of the ROADMAP §12 "SmartStore single-product authoring AI" stage, after `recommended_name` (ADR-0027):
- tag/search-signal candidate registration;
- the official-evidence review;
- endpoint adoption, only where the evidence is sufficient;
- the SearchSignalAdapter;
- `recommended_tags`.

It lands before its code. Each implementation slice cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219 comment `6072888750`. It extends the exception of `6067968983` to the rest of this stage, in the ROADMAP §12 order, contract-first and DRY_RUN.
- **Already decided by canon:**
  - Canonical v3.1 §7.6 (`recommended_tags`, `final_tags`), §7.8 (the tag direction) and §18 (a SearchSignal source is an official API or permitted data only);
  - ADR-0012 (the provider port), ADR-0026 (the foundation), ADR-0027 (the CLIProxyAPI provider);
  - `documents/contracts/platforms/smartstore/ENDPOINT_MATRIX.md` §16 (change control).
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- this contract;
- the two `NOT_ADOPTED` candidate rows of `ENDPOINT_MATRIX.md` §4 and their evidence review (§4.3);
- the slices of §8.

What it does not authorize:
- **No tag is sent to the marketplace.**
  - The SmartStore CREATE contract (ENDPOINT_MATRIX §4.1.1) still freezes tags without sending them (ADR-0014).
  - Projecting `originProduct.detailAttribute.seoInfo.sellerTags` into CREATE is a separate amendment of that contract, with its own evidence and owner decision.
- **No external search-signal source.**
  - The SearchSignalAdapter port exists, but no source is selected.
  - Each candidate source needs its own credentials and terms, so each is a separate owner decision. Examples: the NAVER 검색광고 keyword tool, the NAVER DataLab shopping insight.
- **No AI_INITIAL**, no automatic application, and no other stage. Category, options, notices, Coupang, 11st and bulk AI keep their order and gate.
- **No LIVE.** The two adopted endpoints are reads.

Sources:
- the 2.90.1 research packet in Issue #219 comment `6072975577`, which covers:
  - `[상품] 추천 태그 검색 목록 조회` — https://apicenter.commerce.naver.com/docs/commerce-api/current/get-recommend-tags-product;
  - `[상품] 제한 태그 여부 조회` — https://apicenter.commerce.naver.com/docs/commerce-api/current/is-restrict-tags-product;
  - the create-product `sellerTags` schema;
  - official support discussions #3710, #3711, #676 and #1610;
- `SOURCES.md` §5.9 and §6.

Recorded by: Claude Code (Track A). The number was confirmed free on main and on every open branch.

Date: 2026-10-09

---

## 1. The official evidence and its verdict

**`SMARTSTORE_TAG_RECOMMEND`, `GET /v2/tags/recommend-tags`.**
- Query: `keyword` (string, required).
- `200` answers an array of objects:
  - `code`: integer int64, the tag id, not required;
  - `text`: string, the tag name, required.
- At most 20 tags are returned, the same 20 as the 스마트스토어센터 tag search. There is no pagination, and no way to list every tag (#3710).
- The tag group `/v2/tags/category-recommend-tags` was discontinued on 2023-05-17 (#676). It is not a candidate.

**`SMARTSTORE_TAG_RESTRICTED`, `GET /v2/tags/restricted-tags`.**
- Query: `tags` (string array, required).
- `200` answers an array of objects:
  - `tag`: string;
  - `restricted`: boolean.
- `restricted: true` means the tag cannot be used in `sellerTags.text`, and a create or update carrying it fails (#3711).
- `false` is not a guarantee. Category, brand or seller-name rules may still refuse the tag, the create or update result is final, and tags are refreshed periodically. So a check is a snapshot, re-made right before any send (#3711).

**Both endpoints.**
- They sit in the 상품 › 태그 documentation group, with OAuth bearer authentication and no scopes.
- The documented errors are `308`, `400`, `401`, `403`, `404` and `500`, each with `code`, `message`, `invalidInputs[]` and `timestamp`.
- `429` `GW.RATE_LIMIT` comes from the global token bucket.

**CREATE.** `originProduct.detailAttribute.seoInfo.sellerTags` is `object[]` of `{code?: int64, text}`.
- A `code` that does not match its `text` fails the request.
- A directly entered tag carries no `code`.
- The page shows `<= 4000 characters` on the array.

**Verdict: SUFFICIENT for read-only adoption of both endpoints.** These gaps are recorded, and each is closed by its rule here or by runtime evidence (`R0`) at adoption:
- **API group.** Only the documentation grouping (상품) names it. The first adopted read confirms it; a `GW.AUTHN` means the group is missing (`NAVER-P1-PRODUCT-GROUP-1835`).
- **`tags` serialization and maximum count.** These are not documented. ICBM sends the array as repeated `tags` query parameters, at most 10 per call. The first adopted read is `R0` evidence that every requested tag is answered once.
- **Rate.** No number is documented. Reads are paced, and a `429` is `RATE_LIMITED` with bounded backoff (ERRORS.md).
- **Keyword length.** This is not documented. ICBM sends at most 50 characters.

## 2. The two read contracts (adopted in T2)

| Field | `SMARTSTORE_TAG_RECOMMEND` | `SMARTSTORE_TAG_RESTRICTED` |
| --- | --- | --- |
| Method / path | `GET /v2/tags/recommend-tags` | `GET /v2/tags/restricted-tags` |
| Request | exactly `keyword`, a non-blank string of at most 50 characters | `tags` repeated 1–10 times, each a non-blank string of at most 50 characters, distinct |
| App mode / group | `OWN_STORE_SELF` / `상품` | `OWN_STORE_SELF` / `상품` |
| Mutation | none; a read, allowed in DRY_RUN like the other adopted reads | none |
| Success predicate | HTTP 200 and a JSON array (at most 20) of objects, each with a non-blank string `text`, and with `code`, when present, a JSON integer (never a `bool`) in the int64 range | HTTP 200 and a JSON array of objects, each with a string `tag` and a boolean `restricted`, where every requested tag is answered exactly once and nothing else is |
| Otherwise | `ERROR`, classified as in ERRORS.md; a `3xx` is never followed | the same; a tag not answered is **not checked**, never "not restricted" |
| Retention | the `(code, text)` pairs only | the `(tag, restricted)` pairs only |

**Common rules.**
- Both go through the registry-gated SmartStore caller with the CONNECT owner's committed bearer only.
- The mapping revision moves to `ai-tags-r1`. The permission attestation is not invalidated, because only an application or group change does that (owner decision `6055670693`).

## 3. The SearchSignal port

`SearchSignalAdapter.signals(keywords) -> SearchSignals` is the ADR-0012-style port for external search evidence, such as volumes and trends per keyword.
- **Provider-zero.** No source is configured, so it answers `NOT_CONFIGURED` with no signals. The capability `search_signal` is `NOT_CONFIGURED` and never fails core readiness.
- **The tag task runs without it.** Its input records "no signals" as such, never as an empty source that answered.
- **A future source** is adopted only by its own owner decision and evidence:
  - an official API or permitted data (Canonical §18);
  - its credentials in the OS secret store (ADR-0012 §2);
  - loopback or a granted egress host.

## 4. The tag task (`recommended_tags`)

The task is the v29 bundle `TASK_PRODUCT_RECOMMEND_BUNDLE_V1` with the result key `tags`, schema `tag-stage-1`.

**Targeted subject.** It needs a SmartStore target (`marketplace_key`, `marketplace_account_id`). Platform tags are the marketplace's, and the reads need its CONNECT session.

**The deterministic pipeline (Canonical §7.8), in the `enrich.tasks` job, never inline:**
1. **Keywords.** These come from the product's confirmed facts, at most 3 distinct queries:
   - the `original_name` with bracketed text, quantities and units removed;
   - the `brand`;
   - the first two words of the cleaned name.
   Each is trimmed to 50 characters. No fact means no query.
2. **Platform candidates.**
   - `SMARTSTORE_TAG_RECOMMEND` is called for each keyword, and the answers are unioned by `text`, keeping each `code`. The pool holds at most 60 candidates.
   - A failed read fails the task with its error class. A partial pool is never used.
3. **Signals.** These come from the SearchSignal port: none until a source is adopted.
4. **AI.**
   - The composed bundle request carries the facts, the candidate pool and the signals as runtime data.
   - The `tags` output object is `{"recommended": [{"text", "code"?}], "confidence", "evidence", "requires_review"}`, with at most 10 tags.
   - A tag whose `text` is in the pool must carry that pool's `code`. A tag not in the pool is a direct-input tag and carries no `code`.
5. **Deterministic filter.**
   - Texts are trimmed. Exact duplicates are removed, and so are tags equal to the product name or the brand.
   - The rest go through `SMARTSTORE_TAG_RESTRICTED`. A `restricted: true` tag is removed, and so is a tag the check did not answer.
   - The result records `restricted_checked_at`.
6. **Result.**
   - `recommended` is the filtered list. Each tag has `text`, `code` (or `null`) and `source` (`PLATFORM` or `AI_DIRECT`).
   - `requires_review` is true when any tag is `AI_DIRECT` or when filtering removed a tag.
   - An empty list after filtering is a `FAILED` result, `AI_TAGS_NONE_USABLE`, never an empty OK.

**Fingerprint (ADR-0026 §5).**
- It is computed over the declared fact fields (`original_name`, `brand`, `manufacturer`, `origin`, `options`, `detail_description`), the prompt and policy versions, the requested identity, the schema, the candidate pool and the signals.
- A changed pool makes the result stale with the reason `platform`.
- The restricted check is not in the fingerprint. It is a snapshot whose time is shown, and it is re-made before any future send.

**Implementation note (2026-10-09, T3).**
- **Its own id.** The tag task's enrichment id is `SMARTSTORE_TAGS_V1`. It composes the registry task `TASK_PRODUCT_RECOMMEND_BUNDLE_V1` (its `prompt_key`) and records the result key `tags`, so the target-free name result of the same bundle keeps its own subject (ADR-0027 §6).
- **Reuse.** The pool and the signals can be gathered only by the job (§4, never inline).
  - A request therefore always queues the tag task.
  - The job gathers them. When the whole fingerprint, the gathered part included, equals the current result's, it records nothing and calls no AI. That is the reuse.
  - A read derives staleness over the local inputs only and never reads the platform. The gathered part is recorded with the result as `context`, and the next job run compares it.
- **Where the names live.** The marketplace's reads and names live in the AI capability (`app/capabilities/ai/platform_tags.py`) and the composition root. The PRODUCT DB owner stays marketplace-neutral (M4 product contract).
- **The live test of §8 T3** needs the operating instance's SmartStore session, so it follows the owner's 8790 deploy.

## 5. The seed upgrade `v29-ai2`

The bundle's `tags` output object gains the ADR-0026 §5 envelope, `"evidence":[]` and `"requires_review":false`, under the rules of ADR-0027 §6:
- a fresh store is seeded already upgraded;
- an existing store is upgraded only where the field still holds exactly the replaced text, as an audited `RESET` by `system:seed`;
- an operator's edit is never overwritten;
- 기본값 is the newest seed.

## 6. The register owner: tags with a provenance, and their apply

The Preparation's tags today are a bare set (`ListingValues.tags`). They gain one provenance for the set as a whole, exactly as the name has one (ADR-0014 §18):
- **`OPERATOR_CONFIRMED`.** This is what the operator's own save writes. It is locked: an AI value never overwrites it.
- **`AI_SUGGESTION`.** This is written only by the apply. It never satisfies a requirement until the operator's save confirms it.

**The apply.** The ADR-0026 §7 command gains the field `tags`.
- It names the exact result revision and the Preparation revision read, as for the name.
- It writes that result's `recommended` texts as the set.
- A tag's `code` stays with the result, for the future CREATE projection. The Preparation keeps texts only, so nothing about the current CREATE changes.

Track A implements this in the register owner, as for AIF-4, with a handoff note to Track B.

## 7. Screens

- **Settings › AI 공급자.** The card also shows the `search_signal` capability (`NOT_CONFIGURED`, and why).
- **통합DB detail.**
  - The `태그 / AI 상태` group shows the product's targeted `tags` result for the chosen SmartStore account: the tags with their source, the restricted-check time, the review state and the staleness.
  - `✨ AI 추천` also asks the tag task when a SmartStore target is chosen.
- **Editor › 기본정보.** The tag block shows the recommendation and `AI 태그 적용`, which follows the name's apply rules. Tag editing stays inert.

## 8. Implementation order

Each slice is its own PR under the Track A loop.
1. **T1 — this contract.** The candidate rows and the evidence review in ENDPOINT_MATRIX §4.3; `SOURCES.md` §5.9 and §6; ROADMAP §12.
2. **T2 — adoption.**
   - The two reads: registry, predicates and `ai-tags-r1`.
   - The `R0` evidence of one read each, through ICBM's own caller on the operating account. Only structure is recorded, never a value.
3. **T3 — the task.**
   - The SearchSignal port (provider-zero) and the keyword and filter pipeline.
   - The tag task definition and the seed upgrade `v29-ai2`.
   - The live test of one test product through the approved CLIProxyAPI profile.
4. **T4 — the register owner and the screens.** Tags with a provenance, the apply, 통합DB and the editor.

## Invariants

```text
AIT-01  no tag is sent to the marketplace in this stage; CREATE keeps tags frozen and unsent
AIT-02  both tag endpoints are reads through the registry-gated caller; a 3xx is never followed
AIT-03  a tag the restricted check did not answer is never treated as unrestricted
AIT-04  a restricted check is a snapshot with its time; it is re-made before any future send
AIT-05  a platform tag keeps its code; a direct-input tag never carries one
AIT-06  no external search-signal source without its own owner decision and evidence
AIT-07  a seed upgrade never overwrites an operator's edit
AIT-08  an AI tag set is applied only by the operator, as AI_SUGGESTION; a confirmed set is never overwritten
```
