# Use cases

Agents CLI scaffolds, evaluates, and deploys agents from the descriptions you provide to your coding agent. Use it to build:

- **Scheduled bots**: Fetch data from RSS feeds, summarize the results with an LLM, and publish to Google Chat or email on a Cloud Scheduler trigger.
- **Investigation agents**: Read logs, trace deployments, and correlate findings with past incidents to produce a root-cause analysis.
- **Knowledge agents**: Index conversations, email, and design documents so people can find prior decisions when a topic comes up again.
- **A2A multi-agent systems**: Coordinate specialist agents across incident response, code migrations, or audits.

---

## Pick a pattern

Patterns are framework-neutral. The examples assume the default `adk_python` template; for other frameworks see [First-party frameworks](extensions/first-party.md).

<table class="use-case-grid">
<tr>
<td align="center" width="33%"><strong><a href="#daily-news-bot">Daily news bot</a></strong></td>
<td align="center" width="33%"><strong><a href="#industry-watch">Industry watch</a></strong></td>
<td align="center" width="33%"><strong><a href="#self-tuning-support">Self-tuning support</a></strong></td>
</tr>
<tr>
<td align="center"><strong><a href="#technical-investigation">Technical investigation</a></strong></td>
<td align="center"><strong><a href="#regression-detector">Regression detector</a></strong></td>
<td align="center"><strong><a href="#organizational-memory">Organizational memory</a></strong></td>
</tr>
<tr>
<td align="center"><strong><a href="#institutional-memory-navigator">Institutional memory</a></strong></td>
<td align="center"><strong><a href="#due-diligence">Due diligence</a></strong></td>
<td align="center"><strong><a href="#security-audit">Security audit</a></strong></td>
</tr>
<tr>
<td align="center"><strong><a href="#rfp-response-generator">RFP generator</a></strong></td>
<td align="center"><strong><a href="#incident-response-coordination">Incident response</a></strong></td>
<td align="center"><strong><a href="#distributed-code-migration">Code migration</a></strong></td>
</tr>
</table>

!!! note "Not yet supported"

    - **Languages other than Python and Go**: The built-in templates are `adk_python` (Python, the default) and `adk_go` (Go). Java and TypeScript have no built-in template. Other frameworks and languages can ship as templates; see [Authoring](extensions/authoring.md).
    - **Multi-cloud deployments**: Agents CLI focuses on Google Cloud, so working with other clouds may require custom infrastructure and skills.

---

## Beginner

Single-agent patterns with no inter-agent coordination. Suitable as first projects.

### Daily news bot

*Beginner*

Fetch headlines from a configured set of RSS feeds, select the most relevant items with an LLM, and publish to Google Chat or email. Schedule with Cloud Scheduler.

```
Build me a daily news bot that pulls these RSS feeds, summarizes the top 5 stories, and posts to Google Chat every morning.
```

For scheduling and rollout, see [Deployment](deployment.md) and [CI/CD](cicd.md).

### Industry watch

*Beginner*

Track public release notes, documentation updates, job postings, and conference talks across your industry. Report shipped features and hiring trends. Save findings to a store you can query for week-over-week review.

```
Track these companies' public docs, releases, and job postings daily. Surface shipped features and hiring trends.
```

---

## Intermediate

A single agent paired with a feedback loop, retrieval-augmented generation, or substantial tool integration.

### Self-tuning support

*Intermediate*

Run evaluation after each conversation, identify gaps in knowledge or behavior, and draft new evaluation cases for weak responses. The eval dataset grows to cover the questions customers actually ask.

```
Build a support agent that runs eval after each conversation, drafts new eval cases for weak answers, and surfaces documentation gaps.
```

The [Evaluation guide](evaluation.md) describes the eval-and-fix loop. Pair with [observability](observability/index.md) to replay production traces.

### Technical investigation

*Intermediate*

Accept a question such as "Why did latency increase in the payments service last month?" Read logs, trace deployments, and correlate with past incidents. Produce a timeline and root-cause analysis.

```
Build an investigation agent. I ask questions like "why did X break last week" and it pulls from logs, deploy history, and past incidents to produce a writeup.
```

### Regression detector

*Intermediate*

Compare current metrics and log patterns against historical pre-incident signatures. File preventive issues when current behavior matches a known regression pattern. Run on a nightly schedule.

```
Build an agent that runs nightly, looks for metric/log patterns that match historical pre-incident signatures, and files preventive bugs.
```

### Organizational memory

*Intermediate · RAG recipe*

Index Google Chat, email, design documents, and meeting notes for decision records. When a proposal recurs (for example, "use Redis for sessions"), show the original thread and the decision the team reached.

```
Build a RAG agent that indexes Google Chat, email, and design docs nightly. Surface past decisions when someone proposes something we've already discussed.
```

RAG is a clone-and-study recipe. See [Extend the starter agent](development.md#extend-the-starter-agent) for the recipes to adapt.

### Institutional memory navigator

*Intermediate · RAG recipe · Gemini*

Deploy in the Gemini app with permissioned access to Drive, Google Chat, and email. Respond to questions such as "How do I get production database access?" with both the documented process and how it works in practice.

```
Build a RAG agent for new-hire questions that knows both official docs and how things actually work. Publish it to the Gemini app.
```

See the [`google-agents-cli-publish`](../reference/skills.md) skill for registration details.

---

## Advanced

Long-running workflows or multi-agent coordination. Requires dedicated infrastructure and extended development.

### Due diligence

*Advanced · RAG recipe*

Index a target codebase of approximately 500,000 lines. Analyze technical debt, security vulnerabilities, license compliance, and deployment complexity. Produce a risk report with line numbers, dependency graphs, and CVE references. Multi-day analysis benefits from the extended sessions and checkpointing of Agent Runtime in Gemini platform.

```
Build a due-diligence agent that indexes a target codebase, runs security and license scans, and produces a risk report with citations.
```

### Security audit

*Advanced*

Map data flows across the codebase to verify GDPR, HIPAA, or SOC2 compliance. Trace sensitive data from ingestion through deletion. Flag gaps such as analytics logs that retain user data beyond the configured retention policy.

```
Build a compliance-audit agent that traces sensitive data flows across our codebase and flags retention/policy gaps with file:line citations.
```

Use [BigQuery Agent Analytics](observability/bq-agent-analytics.md) to track audit trail completeness.

### RFP response generator

*Advanced · RAG recipe*

Pull from past project records, current resource availability, and pricing models. Estimate timelines and budgets. Draft a technical approach. Produce a proposal package for human review.

```
Build a RAG agent that drafts RFP responses by pulling from past proposals, current resourcing, and pricing models.
```

### Incident response coordination

*Advanced (A2A)*

Run specialist agents in parallel during an outage: one bisects recent changes, one correlates errors across services, one searches past incidents, and one drafts customer communications. Running them in parallel finds the cause faster than troubleshooting one step at a time.

```
Build an A2A multi-agent system for incident response. Specialists for bisection, error correlation, past-incident lookup, and customer comms, coordinated in parallel.
```

The `adk_python` and `adk_go` templates expose the A2A protocol out of the box. Each specialist runs as a service, and the coordinator orchestrates execution.

### Distributed code migration

*Advanced (A2A)*

Run specialist agents for a large framework migration: one handles data models, one handles API contracts, one handles tests, and one handles validation. Specialists coordinate over A2A to share findings about breaking changes. GKE gives you full Kubernetes control when you run many concurrent specialist instances.

```
Build A2A specialist agents for a large framework migration: data models, API contracts, tests, validation.
```

---

## Next steps

- [Tutorial: Build your first agent](quickstart-tutorial.md): Build, evaluate, and deploy with your coding agent
- [Project structure](project-structure.md): What each generated file does
- [Extend the starter agent](development.md#extend-the-starter-agent): The recipes your coding agent studies to add capabilities
- [Development guide](development.md): The full development workflow
- [CLI reference](../cli/index.md): All commands and flags
