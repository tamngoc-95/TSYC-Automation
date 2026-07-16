# TSYC-Automation
# Tiệm Sách Yêu Con Automation

Automation platform for collecting, validating, enriching, reviewing, pricing, and preparing book product data for WooCommerce.

The system is designed for the **Tiệm Sách Yêu Con** online bookstore and supports a controlled workflow using:

* Supabase PostgreSQL as the central database
* Python for collection, ETL, validation, pricing, and automation
* Claude for semantic extraction, product matching, content generation, and exception analysis
* Google Sheets as a future manager review interface
* WooCommerce as the destination for draft products

---

## 1. Project Status

Current implementation stage:

* [x] Initial architecture defined
* [x] Supabase selected as the central database
* [x] Initial database schema designed
* [ ] Supabase project created
* [ ] Initial SQL migration executed
* [ ] Python repository layer implemented
* [ ] Pilot batch imported
* [ ] Claude API integrated
* [ ] Website collectors implemented
* [ ] Facebook collector implemented
* [ ] Google Sheets review interface implemented
* [ ] WooCommerce draft generation implemented
* [ ] Scheduling and monitoring implemented

Current pilot batch:

```text
FB-2026-001
```

---

## 2. Main Objectives

The project aims to:

1. Collect authorized Facebook bookstore posts and approved source URLs.
2. Identify individual books, book sets, and combos.
3. Create one candidate record for each distinct book or ISBN.
4. Collect metadata from approved reference sources.
5. Validate ISBN, dimensions, weight, duplicate records, and required fields.
6. Use Claude for semantic matching and content generation.
7. Calculate provisional costs and suggested selling prices.
8. Send ambiguous or incomplete records to a manager review queue.
9. Create WooCommerce draft products.
10. Keep a complete audit log of automated and manual actions.

---

## 3. System Architecture

```text
Authorized Sources
    |
    v
Python Collectors
    |
    +--> Facebook Collector
    +--> Publisher Collector
    +--> Supplier Collector
    +--> Bookstore Collector
    +--> Fahasa Reference Collector
    |
    v
Supabase PostgreSQL
    |
    +--> Raw source data
    +--> Product candidates
    +--> Reference metadata
    +--> Product images
    +--> Pricing results
    +--> Review issues
    +--> Process logs
    |
    v
Claude
    |
    +--> Candidate extraction
    +--> Reference matching
    +--> Content generation
    +--> Exception analysis
    |
    v
Manager Review
    |
    v
WooCommerce Draft Products
```

---

## 4. Responsibility Separation

### Supabase

Supabase is the system of record.

It stores:

* Batches
* Authorized source URLs
* Raw collected pages
* Product candidates
* Product references
* Product images
* Content versions
* Pricing calculations
* Review issues
* Manager approvals
* WooCommerce synchronization results
* Process logs

Supabase must not:

* Control a browser
* Crawl Facebook directly
* Generate product descriptions
* Decide which conflicting metadata is correct
* Publish WooCommerce products automatically

### Python

Python handles deterministic and testable operations.

Main responsibilities:

* Read authorized source URLs
* Collect website and Facebook data
* Normalize source metadata
* Validate ISBN checksums
* Validate numeric fields and dimensions
* Detect duplicates
* Calculate pricing
* Download and validate images
* Call Claude APIs
* Validate Claude JSON responses
* Write data to Supabase
* Create review issues
* Generate WooCommerce drafts
* Handle retries and resumable processing
* Write process logs

### Claude

Claude handles tasks requiring semantic understanding.

Main responsibilities:

* Extract book candidates from Facebook posts
* Separate combos into individual products
* Match books across different sources
* Identify possible edition conflicts
* Explain metadata conflicts
* Generate short and long descriptions
* Generate key features and recommended age groups
* Suggest product categories and tags
* Analyze ambiguous image-to-product mappings
* Explain why manual review is required

Claude must not:

* Invent ISBN values
* Override manager-approved data
* Confirm image usage rights
* Calculate final prices
* Publish WooCommerce products
* Write unvalidated output directly to the database

### Google Sheets

Google Sheets will be used only as a manager review interface.

It is not the system of record.

Managers may update fields such as:

* Content approval status
* Pricing approval status
* Approved selling price
* Image approval status
* Selected main image
* Manager notes
* Rejection reason

Raw crawler fields must remain read-only.

---

## 5. Source Priority Rules

### Book Identity

Preferred order:

1. Publisher
2. Authorized supplier
3. Reliable bookstore
4. Fahasa
5. Facebook post

Publisher confirmation is preferred but not mandatory when the store does not purchase directly from the publisher. Reliable bookstores and approved suppliers may also be used to verify ISBN and metadata.

### Purchase Price

Preferred order:

1. Purchase invoice
2. Confirmed purchase order
3. Supplier quotation
4. Current supplier price list

Fahasa must not be used as the official purchase-price source.

### Weight

Preferred order:

1. Actual weight measured after arrival in Germany
2. Weight measured before shipment
3. Publisher data
4. Approved supplier data
5. Fahasa reference weight
6. Estimated category weight

### Images

Preferred order:

1. Store-owned images
2. Publisher-approved images
3. Supplier-approved images
4. Reference-only images
5. Images with unknown rights

Claude must never change an image from `RIGHTS_UNKNOWN` to an approved rights status.

---

## 6. Fahasa Usage Policy

Fahasa is used only as a reference source for:

* Book identity
* ISBN verification
* Dimensions
* Estimated weight
* Cover images
* Book introductions
* Cover price reference

Fahasa is not used as:

* The official purchase source
* The official purchase-price source
* Proof of image usage rights
* The automatic final authority when sources conflict

---

## 7. Workflow

### Stage 0 — Batch Creation

Create a batch and register authorized source URLs.

Example:

```text
BatchCode: FB-2026-001
BatchStatus: CREATED
```

### Stage 1 — Source Collection

Python collects raw text, metadata, images, timestamps, and hashes from authorized sources.

Output tables include:

* `source_urls`
* `raw_pages`
* `product_images`
* `process_logs`

### Stage 2 — Candidate Extraction

Claude receives raw Facebook post content and returns structured candidate records.

Python validates the returned JSON before saving it.

### Stage 3 — Reference Collection

Python collects metadata from:

* Publishers
* Authorized suppliers
* Reliable bookstores
* Fahasa

Each source is stored separately in `product_references`.

Reference data must not directly overwrite verified candidate data.

### Stage 4 — Matching and Identity Validation

Claude compares candidate data against multiple reference sources.

Python then performs deterministic validation such as:

* ISBN checksum
* Duplicate ISBN detection
* Duplicate candidate detection
* Weight range checks
* Dimension checks
* Numeric validation

### Stage 5 — Product Content Generation

Claude generates:

* Short description
* Long description
* Key features
* Recommended audience
* Age group
* Categories
* Tags

Python validates the output before saving a new content version.

### Stage 6 — Image Processing

Python:

* Downloads images
* Calculates hashes
* Detects duplicates
* Checks resolution and file type
* Stores files in Supabase Storage
* Saves image metadata in PostgreSQL

Claude may assist with image classification, but manager approval or source rules determine publishing eligibility.

### Stage 7 — Pricing

Python calculates provisional pricing using the active pricing configuration.

Pricing output may include:

* Suggested minimum price
* Suggested maximum price
* Provisional selling price
* Data-completeness status
* Pricing warnings

The final selling price requires manager approval.

### Stage 8 — Manager Review

Candidates requiring review are displayed through a review interface.

Manager approval areas:

* Identity
* Content
* Pricing
* Images
* Main image
* Final notes

### Stage 9 — WooCommerce Draft

Python creates WooCommerce products with:

```text
status = draft
```

Publishing requires all mandatory approvals and validations.

---

## 8. Workflow Statuses

### Batch Status

```text
CREATED
FACEBOOK_COLLECTING
FACEBOOK_COLLECTED
CANDIDATES_EXTRACTED
REFERENCES_COLLECTING
REFERENCES_COLLECTED
AI_ENRICHMENT_RUNNING
MANAGER_REVIEW
DRAFT_GENERATION
COMPLETED
COMPLETED_WITH_WARNINGS
FAILED
```

### Candidate Status

```text
EXTRACTED
IDENTITY_PENDING
IDENTITY_VERIFIED
IDENTITY_CONFLICT
CONTENT_PENDING
CONTENT_READY
CONTENT_APPROVED
PRICE_PENDING
PRICE_APPROVED
IMAGE_PENDING
IMAGE_APPROVED
READY_FOR_DRAFT
DRAFT_CREATED
READY_TO_PUBLISH
PUBLISHED
REJECTED
```

---

## 9. Repository Structure

```text
tsyc-automation/
├── src/
│   ├── collectors/
│   │   ├── facebook_collector.py
│   │   ├── publisher_collector.py
│   │   ├── supplier_collector.py
│   │   └── fahasa_collector.py
│   ├── ai/
│   │   ├── claude_client.py
│   │   ├── candidate_extraction.py
│   │   ├── reference_matching.py
│   │   ├── content_generation.py
│   │   └── exception_review.py
│   ├── repositories/
│   │   └── supabase_repository.py
│   ├── validation/
│   │   ├── isbn_validator.py
│   │   ├── candidate_validator.py
│   │   └── reference_validator.py
│   ├── pricing/
│   │   └── pricing_service.py
│   ├── images/
│   │   ├── image_downloader.py
│   │   └── image_validator.py
│   ├── review_sync/
│   └── woocommerce/
├── migrations/
│   └── 001_initial_schema.sql
├── prompts/
│   ├── facebook_candidate_extraction.md
│   ├── reference_matching.md
│   ├── content_generation.md
│   └── exception_review.md
├── config/
│   └── pricing.example.yaml
├── tests/
├── scripts/
├── .env.example
├── .gitignore
├── requirements.txt
├── run_pipeline.py
└── README.md
```

---

## 10. Environment Configuration

Create a local `.env` file based on `.env.example`.

Example:

```env
SUPABASE_URL=
SUPABASE_SECRET_KEY=
SUPABASE_STORAGE_BUCKET=product-images

ANTHROPIC_API_KEY=

WOOCOMMERCE_URL=
WOOCOMMERCE_CONSUMER_KEY=
WOOCOMMERCE_CONSUMER_SECRET=
```

Never commit the real `.env` file.

The following information must never be stored in GitHub:

* Supabase secret or service-role keys
* Database passwords
* Anthropic API keys
* WooCommerce consumer secrets
* Facebook cookies
* Browser login profiles
* Supplier invoices containing sensitive information
* Customer or personal data
* Production backup files

---

## 11. Local Setup

### Requirements

* Python 3.12 or later
* Git
* Supabase project
* Anthropic API access
* Playwright for browser-based collection
* WooCommerce API credentials at a later stage

### Clone the Repository

```bash
git clone YOUR_REPOSITORY_URL
cd tsyc-automation
```

### Create a Virtual Environment

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

macOS or Linux:

```bash
python -m venv .venv
source .venv/bin/activate
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Configure Environment Variables

```bash
copy .env.example .env
```

Then add local credentials to `.env`.

### Install Playwright Browsers

```bash
playwright install
```

---

## 12. Database Migrations

SQL migrations are stored in:

```text
migrations/
```

The initial migration is:

```text
migrations/001_initial_schema.sql
```

During the initial development phase, migrations are executed manually in the Supabase SQL Editor.

Automatic production database deployment is intentionally disabled until the schema and workflow are stable.

Migration rules:

1. Never modify a migration that has already been applied to production.
2. Create a new migration for every schema change.
3. Use sequential migration names.
4. Test migrations before applying them to production.
5. Keep database changes in Git.

Examples:

```text
001_initial_schema.sql
002_add_product_content_versions.sql
003_add_pricing_tables.sql
004_add_manager_approval_fields.sql
```

---

## 13. Development Rules

### General

* Use English for code comments, docstrings, logs, validation messages, status values, and database annotations.
* Keep business explanations and user-facing documentation in the appropriate language.
* All automated decisions must be traceable.
* Raw source data must not be overwritten.
* Claude output must be validated before database insertion.
* Manager-approved values must not be overwritten automatically.
* Every failed processing step must create a log entry.
* Blocking validation issues must prevent publishing.

### Data Integrity

* One candidate represents one distinct product or ISBN.
* Combos must be separated into individual candidates when appropriate.
* ISBN values must pass checksum validation.
* Duplicate ISBN values must create a review issue.
* Metadata from different sources must be stored separately.
* Verified metadata must include source evidence.
* Image publishing eligibility must depend on usage rights.

### Facebook Collection

* Only process URLs registered in `source_urls`.
* Only process URLs marked as authorized.
* Do not automatically discover unrelated Facebook posts.
* Preserve the original raw post text.
* Store collection timestamps and content hashes.
* Record login and access errors.

---

## 14. Logging

All major operations must write to `process_logs`.

Recommended log levels:

```text
DEBUG
INFO
WARNING
ERROR
CRITICAL
```

Example process names:

```text
CREATE_BATCH
COLLECT_FACEBOOK_POST
EXTRACT_CANDIDATES
COLLECT_REFERENCE
MATCH_REFERENCE
VALIDATE_ISBN
GENERATE_CONTENT
DOWNLOAD_IMAGE
CALCULATE_PRICE
CREATE_REVIEW_ISSUE
CREATE_WOOCOMMERCE_DRAFT
```

Log messages must be written in English.

---

## 15. Testing Strategy

The project should include tests for:

* ISBN-10 validation
* ISBN-13 validation
* Duplicate candidate detection
* Duplicate ISBN detection
* Weight conversion
* Dimension normalization
* Pricing calculations
* Claude JSON schema validation
* Supabase repository operations
* Workflow status transitions
* Image hashing
* WooCommerce payload validation

Run tests with:

```bash
pytest
```

---

## 16. Git Workflow

Recommended branches:

```text
main
develop
feature/*
fix/*
```

Examples:

```text
feature/supabase-repository
feature/claude-integration
feature/facebook-collector
feature/woocommerce-draft
fix/isbn-validation
```

Recommended workflow:

```text
Feature branch
    |
    v
Local testing
    |
    v
Commit and push
    |
    v
Pull request
    |
    v
Review
    |
    v
Merge into develop
    |
    v
Merge stable releases into main
```

Initial commit message:

```text
Initial project structure and Supabase schema
```

---

## 17. Security

Security requirements:

* Keep all repositories private during initial development.
* Never commit secrets.
* Use `.env` only for local development.
* Enable Row Level Security on exposed Supabase tables.
* Use the Supabase secret key only in trusted backend code.
* Use restricted API credentials where possible.
* Do not expose Facebook browser profiles or cookies.
* Do not publish supplier invoices or purchase documents.
* Separate reference images from publish-approved images.
* Review all generated content before public publishing.

---

## 18. Deployment Strategy

Initial execution model:

```text
Local Windows Computer
    |
    +--> Python
    +--> Playwright
    +--> Claude API
    +--> Supabase
```

Later execution model:

```text
Scheduler or External Runner
    |
    v
Python Pipeline
    |
    +--> Website collection
    +--> Claude processing
    +--> Validation
    +--> Supabase update
```

The Facebook collector may continue running locally because it depends on a browser profile and authenticated Facebook session.

Deployment should be introduced gradually:

1. Local development
2. Manual pilot execution
3. Repeatable batch processing
4. Automated tests
5. Staging environment
6. Scheduled website collection
7. Controlled production deployment

---

## 19. Implementation Roadmap

### Phase 1 — Supabase Foundation

* Create the Supabase project
* Apply the initial migration
* Create the private image storage bucket
* Insert the pilot batch
* Register authorized source URLs
* Validate the initial schema

### Phase 2 — Python Repository Layer

Implement:

```text
create_batch()
save_source_url()
save_raw_page()
save_candidate()
save_reference()
save_image()
create_review_issue()
write_process_log()
```

### Phase 3 — Pilot Data Import

* Import existing `FB-2026-001` candidate data
* Validate candidate counts
* Validate source relationships
* Validate process logs
* Identify missing fields

### Phase 4 — Claude Integration

Implement structured Claude tasks:

* Facebook candidate extraction
* Reference matching
* Product content generation
* Exception review

### Phase 5 — Website Collectors

Start with:

* One publisher
* One authorized supplier
* One Fahasa reference page

### Phase 6 — Facebook Collector

Implement local Playwright collection using an authenticated browser profile.

### Phase 7 — Manager Review

Create a controlled review view and approval workflow.

### Phase 8 — WooCommerce Draft Generation

Create WooCommerce draft products from approved candidates.

### Phase 9 — Scheduling and Monitoring

Add scheduled runs only after the manual pipeline is stable.

---

## 20. Current Next Step

The current next implementation task is:

```text
Create the Supabase project and execute migrations/001_initial_schema.sql.
```

After database validation, implement:

```text
src/repositories/supabase_repository.py
```

The first repository methods will be:

```python
create_batch()
save_source_url()
save_candidate()
write_process_log()
```

---

## 21. License and Repository Access

This project contains private business logic and internal bookstore workflows.

The repository should remain private unless an explicit decision is made to publish selected reusable components.

Copyright:

```text
Tiệm Sách Yêu Con
```
