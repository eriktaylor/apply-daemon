# Candidate Profile — Template

> Run `cp -r my_profile_example my_profile` and customize the files inside.
> The pipeline reads from `my_profile/profile.md` at runtime.
> The entire `my_profile/` directory is gitignored — your data stays local.
>
> **How this file is used:**
> - Everything from the top through "What I don't want" is sent directly to the
>   local LLM as candidate context during triage. Write naturally — the model
>   reads it like a person would. Richer, more specific descriptions produce
>   better matching.
> - "Pipeline Settings" is parsed programmatically for configuration values.
> - "Job Alert Configuration" is for manual reference only — the pipeline ignores it.

---

## Who I am

<!-- 2-4 paragraphs covering your background, career trajectory, what you've
     built, and what makes you distinctive. The LLM uses this to understand not
     just your skills but your identity as a professional. More detail = better
     triage on borderline listings. -->

Jane Doe. Based in Seattle, WA. US Citizen — no sponsorship needed.

I'm a backend engineer with 8 years of experience, mostly at mid-stage startups in the developer tools and fintech spaces. I started in data engineering, building ETL pipelines and data warehouses, then transitioned to full-stack product engineering over the last 3 years.

My most impactful work has been at a Series B developer tools company where I architected a real-time event processing system handling 50K events/second, and at a fintech startup where I built the core payment reconciliation pipeline. I'm comfortable owning systems end-to-end from database schema to API design to deployment.

My trajectory is toward senior IC roles at companies where engineering culture values autonomy, deep technical work, and shipping. I'm not interested in management, front-end-heavy roles, or enterprise consulting.

---

## What I'm looking for

<!-- Be specific about role type, location, compensation, and preferences.
     The LLM uses this to rank listings against each other.

     READ docs/PROFILE.md BEFORE EDITING THIS SECTION. The single most common
     mistake is writing for a pass/fail threshold instead of for rank order,
     which collapses everything into one band and makes the ranking useless. -->

### How to score

<!-- Keep this block. Adjust the bands to your own targets, but keep the
     instruction to spread scores and the "everything else" band at the end. -->

**Selection is by rank order within a batch, not by an absolute threshold.
Spread the scores.** If most of a batch lands in the same band, the ranking
carries no information. Given two listings that both look acceptable, say
which is better and why.

Rank by the ladder below, highest band first. Within a band, use the
tie-breakers.

- **Band A — best fit.** Distributed systems and data-pipeline architecture
  where I own the design: event processing, streaming infrastructure, the
  systems other engineers build on top of.
- **Band B — platform and developer tooling.** Internal platforms, developer
  experience, infrastructure-as-product.
- **Band C — backend engineering with real ownership.** Senior/Staff IC roles
  where I own a service end to end rather than implementing tickets.
- **Band D — everything else that survives the literal rejects.** Surface it,
  but rank it last.

**Rank UP within a band:** small focused team · genuinely remote-friendly ·
the listing describes a concrete technical problem rather than a stack list ·
Series A–C.

**Rank DOWN within a band (these are not rejects):** large org where the role
is one narrow slice · heavy on-call or maintenance duty · vague engineering
language with no system described · compensation clearly below $120K.

<!-- Keyword count is not fit. A listing that sprays fashionable vocabulary
     but describes narrow implementation work ranks BELOW one in plainer
     language that clearly hands you design ownership. -->

**Role type:** Senior or Staff-level IC focused on backend systems, platform engineering, or distributed infrastructure. I want hard technical problems with a small, focused team.

**Location:** Seattle metro or remote (US). Will not relocate outside the Pacific Northwest. Portland, OR is acceptable.

**Compensation:** $120K minimum. $180K+ preferred. Above $250K seems unrealistic for my target roles — flag for review.

**Industries:** Developer tools and fintech are my preferred domains. Open to everything else except defense and federal contracting.

**Company stage:** Prefer Series A–C startups. All stages considered.

---

## My skills

<!-- Group by proficiency level. Be honest — the LLM uses this to judge whether
     you're qualified for roles that list specific technologies. -->

**Expert-level:** Python, PostgreSQL, data pipeline architecture, FastAPI, real-time event processing, ETL systems.

**Proficient:** TypeScript, Django, SQLAlchemy, Celery, Redis, AWS (EC2, Lambda, S3, RDS), Docker, CI/CD (GitHub Actions), API design, Kafka, microservices.

**Familiar:** Rust, Kubernetes, React, machine learning (scikit-learn, basic PyTorch), payment processing, Go (currently learning).

---

## What excites me most

<!-- Write this like you're telling a friend what kind of job you really want.
     The LLM matches these signals semantically — you don't need exact keyword
     matches, just clear descriptions of what appeals to you. -->

Distributed systems, data pipeline architecture, real-time event processing, backend infrastructure at scale. I love working on the systems that other engineers build on top of.

Strong signals: Python-heavy backends, PostgreSQL, message queues (Kafka, Redis), event-driven architectures, gRPC, API design, platform engineering, developer tools, infrastructure-as-product.

I get excited about companies where engineering teams have real autonomy, where remote work is genuinely supported, and where the culture values shipping over meetings. Growth-stage startups (Series A–C) are my sweet spot — big enough to have real problems, small enough that my work matters.

Mildly interested in: Rust adoption in backend systems, machine learning infrastructure (not modeling itself), open source, fintech payments.

---

## What I don't want

<!-- Two lists, and the split matters. A rule that needs an exception to be
     correct is not a reject rule — every reject misfire is a good listing
     dropped before you ever see it, silently. Put judgement calls in the
     second list. See docs/PROFILE.md §4. -->

### Literal rejects — decidable without judgement

<!-- Only things readable straight off the listing text: named stacks, named
     products, interview markers, credential gates, geography, seniority. -->

- Junior, entry-level, intern, or new-grad roles.
- Core stack is PHP, .NET, or Java/Scala.
- Enterprise IT products: Salesforce, SAP, Oracle, Workday, ServiceNow.
- Legacy systems: mainframe, COBOL, Hadoop.
- Primarily front-end (React/Angular/Vue) with no backend component.
- Mobile development (iOS/Android) as the primary role.
- Requires an active security clearance.
- Six or more interview rounds, or LeetCode/whiteboard screens stated in the
  process.
- On-site required outside the Seattle metro.

### Rank down, do not reject

<!-- Everything needing a judgement call. These reduce rank but stay in the
     pool — better to review and pass than to miss one. -->

- People management (Manager/Director/VP) — unless the role is explicitly
  hands-on at a small company.
- Data analyst or BI-flavored work with some engineering attached.
- Consulting or agency work.
- "Rockstar" / "ninja" / "guru" culture language, or "unlimited PTO."
- Vague postings that list a stack but never describe a problem.

---

## Pipeline Settings

> Parsed programmatically. Not sent to the LLM.

| Setting | Value | Notes |
|---|---|---|
| max_listings_per_run | 200 | Safety cap per cycle |
| dedup_window_days | 30 | How far back to check for duplicates (active listings) |
| pass_window_days | 180 | How long passed/expired listings stay blocked before resurfacing |
| batch_process_days | 3 | Batch process only saved jobs from the past n days |
| home_location | Seattle, WA | Base location for commute distance calculations |
| max_listing_age_days | 60 | Drop YES/MAYBE listings whose date_posted is older than this many days. Listings with unknown date_posted always pass. Leave blank to disable filter (badge will still render). |

---

## Job Alert Configuration

> Manual reference for setting up email alerts. Pipeline ignores this.

### LinkedIn

- `"backend engineer" OR "staff engineer" OR "platform engineer"` — United States (remote), Mid-Senior, Past week
- `"infrastructure engineer" OR "distributed systems"` — Seattle area, Mid-Senior, Past week

### Indeed

- `"backend engineer" OR "staff engineer" OR "platform engineer"` — Seattle, WA (25mi), Full-time

### Google Alerts

- `"staff engineer" OR "senior backend engineer" remote` — once a day
