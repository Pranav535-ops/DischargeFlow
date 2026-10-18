# DischargeFlow - Product Requirements Document

## Project Overview
DischargeFlow is a hospital discharge management system. It aims to cut the time from "medically ready" to "patient
has left" (often 6–8 hours) toward a target of under 2 hours by coordinating the staff and tasks involved, and to
improve discharge documentation with physician-approved AI summaries.

The original MVP (January 2026, built on the Emergent platform) was rebuilt for production readiness following
[ADR-001](../docs/architecture/ADR-001-production-architecture.md). This document describes the product as of
October 2026.

## User Personas

### Physician
- Starts discharge workflows and completes the final discharge
- Generates, edits and approves AI discharge summaries
- Views patient records, workflow progress and a patient's audit history

### Nurse
- Works discharge tasks (education, vitals, equipment, follow-up, billing clearance)
- Creates and updates patient records and ad-hoc tasks
- Receives notifications for new discharges

### Pharmacist
- Handles medication review and preparation tasks
- Receives notifications for medication tasks

### Admin
- Manages user roles and integration API keys
- Views the audit log and verifies its integrity
- Views operational metrics and AI insights
- Holds every permission, including patient data erasure

## Requirements

### Authentication and access
- Google sign-in through Emergent Auth; new users wait as `pending` (no access) until an admin approves them or invites them by email
- Short-lived access tokens with a rotating refresh cookie; logout and role changes take effect within seconds
- Permission-based access (`workflow:manage`, `summary:manage`, ...) mapped to roles in the database
- Hospitals (tenants) are isolated from each other at the database level

### Patient management
- Create, view and update patients; status Admitted → Pending Discharge → Discharged
- GDPR consent and right to erasure
- Patient identifiers (name, date of birth, diagnosis, MRN) encrypted at rest

### Discharge workflow
- Physician-initiated workflow with eight standard tasks assigned by role
- Automatic status changes as tasks open and close; completion requires all tasks done and an approved summary
- At most one active discharge per patient

### AI discharge summaries
- Generated from de-identified, minimal facts; structured and validated output
- Grounding warnings for facts not in the input
- Physician review, edit and approval before anything reaches the record
- Provenance for every model call; automated evals guard every prompt or model change

### AI assistant
- Natural-language lookups of patients, tasks, analytics and claims
- Writes are proposed and must be confirmed by the user

### Notifications
- In-app, routed by role, delivered live, with read tracking

### Analytics
- Dashboard KPIs and task distribution for everyone
- Operational metrics (throughput, approval wait, task SLA breaches) and AI explanations of aggregate trends for admins

### Insurance
- Claims per patient with status tracking and reports (denial rate, by insurer)

### Compliance
- Append-only, tamper-evident audit log of sensitive actions, with verification
- PHI kept out of logs and out of AI prompts

### Interoperability
- Read-only FHIR R4 API for patients, encounters, tasks and approved summaries
- Inbound HL7 v2 ADT: admissions (A01) create patients; discharges (A03) complete the workflow when it is ready

## What's Been Implemented (October 2026)

Everything in the Requirements section above is implemented and deployed at https://dischargeflow.vercel.app, with
synthetic data only. See the [README](../README.md) for features and architecture and [docs/api.md](../docs/api.md)
for the endpoints.

### Tech stack
- **Frontend:** React 19, Vite, TanStack Query, React Router, Tailwind + shadcn/ui (Radix), Recharts
- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic
- **Database:** PostgreSQL 16 (Neon in production; SQLite for local development and tests)
- **AI:** provider-agnostic gateway with a deterministic mock adapter (used in production today) and Claude via the
  Anthropic API or Amazon Bedrock
- **Auth:** Emergent Google OAuth, then the app's own JWT access tokens and rotating refresh sessions
- **Hosting:** Vercel; Terraform for AWS (ECS Fargate, RDS, CloudFront) is written but not applied

## Backlog

### Waiting on an AWS account and a BAA
- [ ] Real patient data (requires a BAA with the cloud and model provider)
- [ ] Cognito sign-in with MFA for admins and physicians
- [ ] Key-encryption key in AWS KMS
- [ ] Asynchronous AI jobs (outbox → SQS → worker)
- [ ] Apply the Terraform in `infra/`

### Product
- [ ] Email/SMS notifications (in-app only today)
- [ ] Medication management integration
- [ ] Follow-up appointment scheduling
- [ ] Printable discharge documents
- [ ] Customizable workflow templates
- [ ] Admin screen for integration clients (API only today)
- [ ] More HL7 ADT events (A02 transfer, A08 update, A11/A13 cancellations) and a FHIR write API
- [ ] Mobile app, multi-language support, custom branding
