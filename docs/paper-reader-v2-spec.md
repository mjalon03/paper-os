# Paper Reader v2 Quality Specification

## Goal

Replace the v1 coordinate-block reader with a structured article pipeline that
is pleasant to read on a phone. The new pipeline must remove publication
furniture, preserve complete paragraphs and section hierarchy, and show actual
figures instead of full-page screenshots.

The existing v1 output remains available until v2 passes the representative
paper review. A v2 conversion must never modify the immutable raw PDF.

## Source strategy

Use the best structured source available for each paper, in this order:

1. Resolve a DOI to a PMCID with the official PMC ID Converter.
2. If an eligible PMC article version exists, read its current S3 metadata
   object and use the referenced JATS XML and media objects.
3. Otherwise, run the approved local PDF structure engine.
4. Retain PyMuPDF only for bounded source-page rendering and coordinate-based
   crops, not as the primary source of article structure.

PMC completed its Article Dataset distribution migration on 2026-08-26. New
code must use the anonymous `pmc-oa-opendata` S3 layout and must not depend on
the removed legacy OA package API. The article-version JSON object is the
authority for the XML, media URLs, version type, retraction state, and license.

Remote structured content is an optional enhancement. Failure, ineligibility,
license uncertainty, or a DOI miss must fall back to local PDF processing
without losing the paper.

## Canonical article model

Every extractor must produce the same versioned intermediate representation:

- article identity and source provenance;
- abstract;
- nested sections with stable IDs and heading levels;
- complete paragraphs with source-page provenance where available;
- lists, equations, tables, figures, captions, and inline cross-references;
- references outside the default reading flow;
- excluded publication furniture with a reason, retained only for diagnostics;
- extraction warnings and quality signals.

The renderer consumes only this model. It must not contain engine-specific PDF
block logic.

## Required content behavior

### Main reading flow

- Exclude running headers, footers, page numbers, journal chrome, copyright
  boilerplate, author affiliations, correspondence blocks, and submission
  history from the main body.
- Keep author and publication metadata in the article header.
- Keep references available on demand but outside the default reading flow.
- Preserve section and subsection hierarchy.
- Preserve the exact scientific wording. Automated cleanup may join or split
  blocks but must not paraphrase, summarize, or invent claims.
- Join line wraps, column breaks, and page breaks into complete paragraphs.
- Repair line-break hyphenation only when the joined token is supported by the
  source text and surrounding layout.

### Figures and tables

- Display only the figure or table asset, never an automatic full-page image.
- Prefer publisher/PMC media when the article metadata permits retrieval.
- For PDF fallback, crop a verified figure bounding box at readable resolution.
- Keep the complete caption with the asset.
- Place the figure near its first body reference when that relationship is
  available.
- Provide an explicit source-page link as a fallback diagnostic action.
- If a confident figure crop is unavailable, show the caption and source-page
  action without embedding the whole page in the reading flow.

## Mobile reading behavior

- Open with article metadata, abstract, reading progress, and a clear start
  action.
- Provide a section-aware table of contents and show the current section.
- Remember reading position per paper and reader version.
- Support readable type scaling, light/dark themes, and figure zoom.
- Allow references, methods, supplementary content, and figure captions to be
  expanded or collapsed without deleting them.
- Offer a direct original-PDF action without making the PDF the primary reader.
- Maintain a comfortable single-column measure and avoid persistent controls
  that consume substantial phone-screen height.

## Representative benchmark set

| Case | Paper | Reason | Structured-source status |
| --- | --- | --- | --- |
| PDF fallback | *Deep learning* (2015) | Dense Nature two-column review with publication furniture | DOI not found in PMC |
| Published JATS | *Comparative Effects of Event Detection Methods...* (2021) | Research article with eight figures and four tables | PMC8032960, CC BY |
| Preprint JATS | *AQuA2-Cloud* (2026) | Recent methods preprint with six figures and one table | PMC13060981, CC BY |

After captions and back matter are separated, the two JATS fixtures produce 26
sections, 46 body paragraphs, 8 figures, 4 tables, and 48 references for the
published article; and 11 sections, 24 body paragraphs, 6 figures, 1 table, and
22 references for the preprint. These counts are benchmark observations, not
hard-coded production expectations.

### PDF fallback benchmark result

Docling 2.71 was selected as the local PDF fallback engine after conversion of
the nine-page *Deep learning* benchmark. It recovered two-column reading order,
paragraph-sized text units, headings, captions, and bounded picture regions far
more reliably than the v1 coordinate-block extractor. Deterministic cleanup is
still required for running-header picture detections, front matter, references,
drop caps, and multi-panel figure grouping.

The same conversion completed on the OVH host in 64 seconds with one CPU and a
2.5 GB container memory limit. Peak resident memory was 1.79 GB. The worker
therefore needs a 2.5 GB limit, one-article-at-a-time concurrency, and must stay
separate from the small API image. Its Linux image must install CPU-only PyTorch
before Docling and include `libxcb1`, `libglib2.0-0`, and `libgl1`.

## Acceptance gates

All three representative papers must pass before v2 is enabled for the full
library:

1. No repeated running header, footer, or standalone page number appears in
   the main reading flow.
2. Manual review finds no column-order inversion in the selected abstract,
   introduction, results, and discussion samples.
3. Reviewed sentences are not truncated at line, column, or page boundaries.
4. Headings form an ordered hierarchy and the table of contents follows it.
5. Every displayed benchmark figure is an individual media asset or a bounded
   crop; zero full-page images appear inline.
6. Captions remain associated with the correct figure or table.
7. References do not interrupt the default body flow and remain accessible.
8. Original PDF hashes remain unchanged.
9. Extraction failure is explicit and retryable; it never publishes a partial
   result as ready.
10. Automated tests cover source resolution, untrusted XML, path confinement,
    paragraph integrity, figure association, HTML escaping, and v1/v2
    coexistence.
11. Phone-width visual review confirms readable type, navigation, figure zoom,
    collapsed secondary material, and reading-position restoration.

## Rollout

1. Benchmark source resolution and candidate PDF engines without changing the
   live reader.
2. Implement and test the canonical article model and JATS path.
3. Select the PDF fallback engine from measured quality and resource use.
4. Implement the v2 renderer and inspect the three benchmark papers.
5. Deploy v2 beside v1 under versioned output paths.
6. Ask for user review of the three benchmark papers.
7. Convert the remaining library only after that review passes.

## Staged deployment verification

Reader v2 version 0.5.0 was deployed beside v1 on 2026-08-29. The library's
existing `/read/<id>` links still serve v1; the review cohort is available at
`/read/<id>/v2`. The dedicated worker is restricted to IDs 1, 15, and 21 until
manual review passes.

| ID | Source | Sections | Body paragraphs | Figures | Tables | References |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | Docling PDF | 8 | 50 | 5 | 0 | 114 |
| 15 | PMC JATS | 26 | 46 | 8 | 4 | 48 |
| 21 | PMC JATS | 11 | 24 | 6 | 1 | 22 |

Server verification confirmed that all three raw PDF hashes still match the
registry, every referenced figure asset exists, and no `page-*` image is used
as an inline v2 figure. The API, v1 worker, v2 worker, and PostgreSQL containers
remained healthy; the Docling container did not restart or report an OOM.

Full-library conversion remains gated on manual phone review of paragraph and
caption boundaries, scientific symbol rendering, section navigation, figure
legibility/zoom, theme and type controls, reading-position restoration, and
the original-PDF action.
