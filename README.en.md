<div align="center">

# PrintOps — Print Order Agent

**Turn “I need 500 brochures” into a complete, quote-ready, hand-off-ready print order**

[简体中文](README.md) · [English](README.en.md)

[![CI](https://github.com/11122313211/PrintOps/actions/workflows/ci.yml/badge.svg)](https://github.com/11122313211/PrintOps/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)

**Zero dependencies · Local-first · Data stays on your machine · Nothing ships without human confirmation**

![PrintOps workbench](docs/screenshots/workbench.png)

</div>

---

PrintOps is a local-first print order agent for marketing, design, and procurement teams. Describe what you want to print; the agent fills in the product-specific parameters step by step, explains the trade-offs, produces three comparable production plans (with reference prices and lead times), and ends with an order hand-off whose **every field is traceable and human-confirmed**.

> Positioning: a **pre-order intake and communication** tool. It does not place orders automatically, does not replace prepress checks, and does not promise real quotes — every outward-facing action stops before the human-confirmation gate.

**North star**: let a non-printer person turn a vague requirement into a complete, reliable, traceable print order within 10 minutes.

## ✨ Features

**From one sentence to one order**
- 🗣️ Natural-language intake: product type, quantity, size, paper, color, finishing, binding, deadline, budget; supports revisions and negations (“change quantity to 1200”, “no lamination”)
- 📐 Four size semantics: finished size / expanded size / die-cut size / package 3D size; labeled inputs are routed by meaning; inner vs outer box sizes supported
- 🧩 Multi-product orders: each item gets a stable `itemId` with its own parameters, plan, and hand-off state

**Professional rules up front (explainable, versioned)**
- 🏭 Gang-run vs dedicated press: per-plan print mode, batch color-shift caveats, spot-color limits, dedicated-press premium factor
- 💰 Reference prices: sample price parameter table (category base × quantity tier × finishing factor × print-mode factor, versioned) — explicitly “not a quote”
- 📏 Minimum-quantity hints, imposition hints (sheet counts and utilization on 大度/正度 sheets), saddle-stitch page-count rules, and other trade practices
- 🧾 Category-specific parameters: a 16-product catalog (business cards, labels, boxes, bags, cups, wide-format prints…) with category-aware follow-up questions

**Explainable, confirmable**
- 🔍 Field provenance: every field records its source (user / rule / model) and confidence; low-confidence production fields block the hand-off until confirmed
- 🧭 Run traces: per-run step events (perceive → plan → tools) are inspectable; knowledge versions are traceable
- ✋ Human-confirmation gate: hand-offs and quote requests require explicit confirmation; suppliers are never contacted automatically

**Files & export**
- 📄 In-browser PDF pre-check (page count, page boxes, color-space and font-embedding clues); the artwork never leaves your machine
- 📦 Hand-off text + JSON / CSV / Markdown export

## 🖼️ UI Preview

| Plan comparison | Mobile (390px) |
| --- | --- |
| ![Plan comparison](docs/screenshots/plan-comparison.png) | <img src="docs/screenshots/mobile-390.png" width="260" alt="Mobile"> |

## 🚀 Quick Start

Requires **Python 3.9+**. No third-party packages.

```bash
git clone https://github.com/11122313211/PrintOps.git
cd PrintOps
python server.py          # macOS / Linux; on Windows run start_windows.bat
```

Open <http://localhost:4174/> — health check: <http://localhost:4174/api/health> → `{"ok": true}`

> 🔐 On first start the terminal prints a **local access token** (`X-PrintOps-Token`). The served page carries it automatically; for curl add `-H "X-PrintOps-Token: <token>"`.

<details>
<summary><b>Port conflicts</b></summary>

```bash
# macOS / Linux
lsof -nP -iTCP:4174 -sTCP:LISTEN
# Windows PowerShell
Get-NetTCPConnection -LocalPort 4174 -State Listen
```

Dev fallback: `PRINTOPS_PORT=4174 python server.py` (commits and acceptance always use 4174).

</details>

## 💬 Examples

| You say | The agent does |
| --- | --- |
| 500 A4 brochures, 32 pages saddle-stitched, 157g matte paper, full color both sides, next week | Builds the order → three plans (print mode / reference price / lead time comparison table) → waits for your pick |
| A batch of 5×5cm round clear vinyl stickers | Recognizes the small size plus label material/shape; asks about adhesive and use case |
| 500 lid-and-base boxes, 60*40*20cm, inner size | Recognizes box structure and 3D size; stores the inner-size semantics |
| Change quantity to 1200, no lamination | Updates the order, invalidates stale plans, re-recommends |

The plan section renders a **side-by-side comparison table** (print mode / material / finishing / binding / reference price / lead time / fit) with the recommended column highlighted. Reference prices come from a versioned sample price table and are explicitly not quotes.

## 🧠 How It Works

```
understand → clarify category → complete parameters → recommend plans → preflight files
          → prepare quotes → human confirmation → platform hand-off / export
```

**Division of labor: the model understands and explains, rules constrain, tools execute, humans confirm high-risk results.**

| Module | Responsibility |
| --- | --- |
| `nlu.py` | Rule-based perception: extraction + confidence grading (explicit evidence ≥0.9, weak inference <0.75 requires confirmation) |
| `order_model.py` | Order data contract, quantity/size normalization, `schemaVersion` migrations |
| `tools.py` | Whitelisted tools: plan recommendation, pricing, supplier capability matching, hand-off, quote preparation |
| `agent.py` | Session memory (SQLite WAL), workflow stage machine, tool gateway, model collaboration (max two rounds) |
| `llm_adapter.py` | Optional OpenAI-compatible planner with automatic rule fallback; built-in SSRF host validation |
| `product_knowledge.py` | Product catalog, print knowledge, sample price parameter table (all versioned with sources) |
| `supplier_adapters.py` | Platform capability profiles and per-category field-mapping protocol |

Full design in [Architecture & data contracts](docs/ARCHITECTURE.md) (Chinese).

## 🔐 Security Model

- **Local access token**: every endpoint except `/api/health` requires `X-PrintOps-Token` (a per-process random token injected into the served page); cross-origin requests get 403
- **Static whitelist**: only `/`, `/index.html`, `/app.js`, `/styles.css` are served; source, docs, and runtime data return 404
- **SSRF protection**: LLM endpoint URLs are validated at configuration time — loopback / private / CGNAT / reserved / link-local hosts are rejected
- **API keys**: stored only in local `data/llm_config.json` (permission 600), never in logs, exports, or Git; the UI warns about plaintext storage — prefer environment variables
- **Durability**: SQLite runs with WAL and a 5s busy timeout; corrupted sessions are quarantined (backed up to `data/corrupted/`) without taking the service down

Known limitations and the hardening backlog live in the [ROADMAP](docs/ROADMAP.md).

## 🧪 Testing & Quality

```bash
python -m unittest discover -s tests -p "test_*.py"   # 149 unit / contract / security tests
python tests/evaluate_agent.py                        # 111-case desensitized order evaluation + real-corpus evaluation
python tools/secret_scan.py                           # secret scanning
```

Release gates: field accuracy ≥95% and completion ≥80% on completable cases (currently both 100%); a hard accuracy gate activates once 20 real desensitized orders are curated. CI runs everything on Ubuntu/Windows × Python 3.9/3.12.

The 1.0 release checklist lives in [RELEASE_CHECKLIST](docs/RELEASE_CHECKLIST.md) (Chinese).

## 🗺️ Roadmap

- **v1.0 (current)**: first stable release — natural-language intake, explainable plans, local security model, controlled export
- **v1.1+**: controlled supplier integration (capability profiles + field adapters → quote drafts → human-confirmed submission), real quote write-back, production lead-time scheduling
- Ongoing: replacing synthetic evaluation cases with real desensitized orders, expanding the trade-rule library, LLM lock and session isolation improvements

See the full [ROADMAP](docs/ROADMAP.md).

## 📖 Documentation

| Document | Contents |
| --- | --- |
| [Architecture & data contracts](docs/ARCHITECTURE.md) | Module responsibilities, field contracts, confidence & migration strategy (Chinese) |
| [Roadmap](docs/ROADMAP.md) | Milestones and the hardening backlog (Chinese) |
| [1.0 release checklist](docs/RELEASE_CHECKLIST.md) | Release gates, real-corpus and manual walkthrough (Chinese) |

## 🤝 Contributing

Issues and PRs are welcome. Before submitting:

```bash
python -m unittest discover -s tests -p "test_*.py"
python tests/evaluate_agent.py
python tools/secret_scan.py
```

## 📄 License

[MIT](LICENSE) © PrintOps contributors

## 🙈 Acknowledgments

The product catalog and parameter structure reference public navigation from Shengda Printing ([sd2000.com](https://www.sd2000.com/)) and similar public sources, used only to align parameter conventions. They do not represent any supplier's live prices, capacity, or lead times.
