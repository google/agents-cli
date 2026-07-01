# Use Cases

Agents CLI scaffolds, evaluates, and deploys agents from the descriptions you provide to your coding agent. Use it to build:

- **Scheduled bots.** Fetch data from RSS feeds, summarize the results with an LLM, and publish to Google Chat or email on a Cloud Scheduler trigger.
- **Investigation agents.** Read logs, trace deployments, and correlate findings with past incidents to produce a root-cause analysis.
- **Knowledge agents.** Index conversations, email, and design documents so that prior decisions are retrievable when topics recur.
- **A2A multi-agent systems.** Coordinate specialist agents across incident response, code migrations, or audits.

---

## Pick a Pattern

<table class="use-case-grid">
<tr>
<td align="center" width="33%"><h3><a href="#daily-news-bot">Daily News Bot</a></h3></td>
<td align="center" width="33%"><h3><a href="#industry-watch">Industry Watch</a></h3></td>
<td align="center" width="33%"><h3><a href="#self-tuning-support">Self-Tuning Support</a></h3></td>
</tr>
<tr>
<td align="center"><h3><a href="#technical-investigation">Technical Investigation</a></h3></td>
<td align="center"><h3><a href="#regression-detector">Regression Detector</a></h3></td>
<td align="center"><h3><a href="#organizational-memory">Organizational Memory</a></h3></td>
</tr>
<tr>
<td align="center"><h3><a href="#institutional-memory-navigator">Institutional Memory</a></h3></td>
<td align="center"><h3><a href="#due-diligence">Due Diligence</a></h3></td>
<td align="center"><h3><a href="#security-audit">Security Audit</a></h3></td>
</tr>
<tr>
<td align="center"><h3><a href="#rfp-response-generator">RFP Generator</a></h3></td>
<td align="center"><h3><a href="#incident-response-coordination">Incident Response</a></h3></td>
<td align="center"><h3><a href="#distributed-code-migration">Code Migration</a></h3></td>
</tr>
</table>

!!! note "Not yet supported"

    - **Real-time voice and video**
    - **Non-Python agents** (Go, Java, TypeScript)
    - **Multi-cloud deployments** — focused on Google Cloud; interaction with other clouds may require custom infrastructure and skills

---

## Beginner

Single-agent patterns with no inter-agent coordination. Suitable as first projects.

### Daily News Bot

*Beginner · `adk`*

Fetch headlines from a configured set of RSS feeds, select the most relevant items with an LLM, and publish to Google Chat or email. Schedule with Cloud Scheduler.

```
Build me a daily news bot that pulls these RSS feeds, summarizes the top 5 stories, and posts to Google Chat every morning.
```

For scheduling and rollout, see [Deployment](deployment.md) and [CI/CD](cicd.md).

### Industry Watch

*Beginner · `adk`*

Track public release notes, documentation updates, job postings, and conference talks across your industry. Surface shipped features and hiring trends. Persist findings to a queryable store for week-over-week review.

```
Track these companies' public docs, releases, and job postings daily. Surface shipped features and hiring trends.
```

---

## Intermediate

A single agent paired with a feedback loop, retrieval-augmented generation, or substantial tool integration.

### Self-Tuning Support

*Intermediate · `adk`*

Run evaluation after each conversation, identify gaps in knowledge or behavior, and draft new evaluation cases for weak responses. Coverage adapts to the questions customers actually ask.

```
Build a support agent that runs eval after each conversation, drafts new eval cases for weak answers, and surfaces documentation gaps.
```

The [Evaluation Guide](evaluation.md) describes the eval-and-fix loop. Pair with [observability](observability/index.md) to replay production traces.

### Technical Investigation

*Intermediate · `adk`*

Accept a question such as "Why did latency increase in the payments service last month?" Read logs, trace deployments, and correlate with past incidents. Produce a timeline and root-cause analysis.

```
Build an investigation agent. I ask questions like "why did X break last week" and it pulls from logs, deploy history, and past incidents to produce a writeup.
```

### Regression Detector

*Intermediate · `adk`*

Compare current metrics and log patterns against historical pre-incident signatures. File preventive issues when current behavior matches a known regression pattern. Run on a nightly schedule.

```
Build an agent that runs nightly, looks for metric/log patterns that match historical pre-incident signatures, and files preventive bugs.
```

### Organizational Memory

*Intermediate · RAG recipe*

Index Google Chat, email, design documents, and meeting notes for decision records. When a proposal recurs (for example, "use Redis for sessions"), surface the original thread and the decision the team reached.

```
Build a RAG agent that indexes Google Chat, email, and design docs nightly. Surface past decisions when someone proposes something we've already discussed.
```

RAG is a clone-and-study recipe — see [Agent Templates](templates.md#rag-retrieval-augmented-generation) for the samples to adapt.

### Institutional Memory Navigator

*Intermediate · RAG recipe · Gemini Enterprise*

Deploy in Gemini Enterprise with permissioned access to Drive, Google Chat, and email. Respond to questions such as "How do I get production database access?" with both the documented process and the current operational reality.

```
Build a RAG agent for new-hire questions that knows both official docs and how things actually work. Publish it to Gemini Enterprise.
```

See the [`google-agents-cli-publish`](../reference/skills.md) skill for registration details.

---

## Advanced

Long-running workflows or multi-agent coordination. Requires dedicated infrastructure and extended development.

### Due Diligence

*Advanced · RAG recipe*

Index a target codebase of approximately 500,000 lines. Analyze technical debt, security vulnerabilities, license compliance, and deployment complexity. Produce a risk report with line numbers, dependency graphs, and CVE references. Multi-day analysis benefits from Agent Runtime's extended sessions and checkpointing.

```
Build a due-diligence agent that indexes a target codebase, runs security and license scans, and produces a risk report with citations.
```

### Security Audit

*Advanced · `adk`*

Map data flows across the codebase to verify GDPR, HIPAA, or SOC2 compliance. Trace sensitive data from ingestion through deletion. Flag gaps such as analytics logs that retain user data beyond the configured retention policy.

```
Build a compliance-audit agent that traces sensitive data flows across our codebase and flags retention/policy gaps with file:line citations.
```

Use [BigQuery agent analytics](observability/bq-agent-analytics.md) to track audit trail completeness.

### RFP Response Generator

*Advanced · RAG recipe*

Pull from past project records, current resource availability, and pricing models. Estimate timelines and budgets. Draft a technical approach. Produce a proposal package for human review.

```
Build a RAG agent that drafts RFP responses by pulling from past proposals, current resourcing, and pricing models.
```

### Incident Response Coordination

*Advanced (A2A) · `adk`*

Run specialist agents in parallel during an outage: one bisects recent changes, one correlates errors across services, one searches past incidents, and one drafts customer communications. Parallel investigation reduces time-to-cause compared to sequential troubleshooting.

```
Build an A2A multi-agent system for incident response. Specialists for bisection, error correlation, past-incident lookup, and customer comms — coordinated in parallel.
```

The [`adk` template](templates.md) (A2A built in) exposes the A2A protocol. Each specialist runs as a service, and the coordinator orchestrates execution.

### Distributed Code Migration

*Advanced (A2A) · `adk`*

Run specialist agents for a large framework migration: one handles data models, one handles API contracts, one handles tests, and one handles validation. Specialists coordinate over A2A to share findings about breaking changes. GKE is the recommended runtime when running many concurrent specialist instances.

```
Build A2A specialist agents for a large framework migration: data models, API contracts, tests, validation.
```

---

## Next Steps

- [Tutorial: Build Your First Agent](quickstart-tutorial.md) — build, evaluate, and deploy with your coding agent
- [Project Structure](project-structure.md) — understand what each generated file does
- [Agent Templates](templates.md) — the `adk` template and the RAG clone-and-study recipe
- [Development Guide](development.md) — full development workflow
- [CLI Reference](../cli/index.md) — all commands and flags
