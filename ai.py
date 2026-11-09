"""PayPilot AI: Gemini integration with a deterministic offline fallback.

Gemini is called over plain HTTPS (stdlib only), so no extra SDK is required.
If GEMINI_API_KEY is missing or the call fails, PayPilot AI still answers from
the user's own data in "offline" mode, and the API reports which mode was used.
"""
import json
import os
import sys
import urllib.error
import urllib.request

# --- Persona (verbatim from the product spec) ---------------------------------
SYSTEM_PROMPT = """You are PayPilot AI, an intelligent financial copilot.
You help users:
- Track expenses
- Create budgets
- Analyze spending
- Manage subscriptions
- Set financial goals
- Improve savings habits
You always introduce yourself as PayPilot AI.
Do not say you are Gemini unless the user explicitly asks.
Keep responses financial-focused, practical, and easy to understand."""

CONTEXT_RULES = """
DATA RULES
- The JSON below is the user's real financial snapshot. Base every number you quote on it.
- If something is not in the snapshot, say you don't have it instead of guessing.
- Amounts are in {currency}. Use short paragraphs and bullet points. Be specific and actionable.
- You are a budgeting copilot, not a licensed advisor. For investments, tax or legal questions, give general education and suggest a qualified professional.
{first_turn}
USER FINANCIAL SNAPSHOT:
{snapshot}"""

SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def load_env(path):
    """Tiny .env loader so python-dotenv is not required."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def gemini_configured():
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def model_name():
    return os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()


class GeminiError(Exception):
    pass


def _call_gemini(system_text, contents, max_tokens=2048, temperature=0.6):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise GeminiError("GEMINI_API_KEY is not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name()}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise GeminiError(f"Gemini returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GeminiError("Could not reach Gemini") from exc

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError("Gemini returned no usable answer") from exc
    if not text:
        raise GeminiError("Gemini returned an empty answer")
    return text


def _system_with_context(ctx, first_turn):
    note = "\nThis is the first message of the conversation: introduce yourself as PayPilot AI.\n" if first_turn else ""
    return SYSTEM_PROMPT + "\n" + CONTEXT_RULES.format(
        currency=ctx.get("currency", "INR"),
        first_turn=note,
        snapshot=json.dumps(ctx, default=str, ensure_ascii=False),
    )


def chat(history, message, ctx):
    """Return (reply_text, mode, note). history = [{'role','content'}] oldest first."""
    first_turn = len(history) == 0
    if gemini_configured():
        contents = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in history
        ]
        contents.append({"role": "user", "parts": [{"text": message}]})
        try:
            return _call_gemini(_system_with_context(ctx, first_turn), contents), "gemini", None
        except GeminiError as exc:
            print(f"[PayPilot AI] Gemini failed, using offline mode: {exc}", file=sys.stderr)
            return offline_reply(message, ctx, first_turn), "offline", str(exc)
    return offline_reply(message, ctx, first_turn), "offline", "GEMINI_API_KEY is not set"


def generate_report(ctx, label):
    """Return (markdown, mode, note) for a monthly AI financial report."""
    prompt = (
        f"Write the monthly financial report for {label} using ONLY the snapshot data. "
        "Format as Markdown with these sections: ## Overview, ## What went well, ## Watch-outs, "
        "## Category insights, ## Budget check, ## Subscriptions, ## Goals, ## 30-day action plan "
        "(3 to 5 specific steps with amounts). Start with one line introducing yourself as PayPilot AI. "
        "Keep it under 450 words and skip a section only if there is no data for it."
    )
    if gemini_configured():
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        try:
            return _call_gemini(_system_with_context(ctx, False), contents, max_tokens=3072, temperature=0.4), "gemini", None
        except GeminiError as exc:
            print(f"[PayPilot AI] Gemini report failed, using offline mode: {exc}", file=sys.stderr)
            return offline_report(ctx, label), "offline", str(exc)
    return offline_report(ctx, label), "offline", "GEMINI_API_KEY is not set"


# --- Offline fallback ----------------------------------------------------------
def _m(ctx, value):
    sym = SYMBOLS.get(ctx.get("currency", "INR"), "")
    if abs(value) >= 100 or float(value).is_integer():
        return f"{sym}{value:,.0f}"
    return f"{sym}{value:,.2f}"


INTRO = "Hi, I'm PayPilot AI, your financial copilot. "


def offline_reply(message, ctx, first_turn):
    text = (message or "").lower()
    intro = INTRO if first_turn else ""
    cur = ctx.get("current", {})
    has_data = bool(cur.get("txn_count"))

    def has(*words):
        return any(w in text for w in words)

    if has("who are you", "what can you do", "introduce") or text.strip() in {"hi", "hello", "hey", "help"}:
        return (
            INTRO + "I help you track expenses, build budgets, analyze spending, manage subscriptions, "
            "set goals and save more.\n\nTry: \"Where did my money go this month?\", \"Which subscriptions should I cut?\" "
            "or \"How do I reach my goals faster?\""
        )

    if not has_data and not ctx.get("goals") and not ctx.get("subscriptions", {}).get("items"):
        return (
            intro + "I don't see any data for you yet. Add a few expenses, a budget or a goal (or load the demo data "
            "from the dashboard) and I can give specific advice based on your numbers."
        )

    if has("subscri", "recurring", "netflix", "cancel"):
        subs = ctx.get("subscriptions", {})
        items = sorted(subs.get("items", []), key=lambda s: -s["monthly_cost"])
        if not items:
            return intro + "You have no active subscriptions tracked. Add them on the Subscriptions page so I can spot savings."
        lines = [f"- {s['name']}: {_m(ctx, s['monthly_cost'])}/month" for s in items[:6]]
        top = items[0]
        return (
            intro + f"You have {len(items)} active subscriptions costing about {_m(ctx, subs['monthly_total'])}/month "
            f"({_m(ctx, subs['monthly_total'] * 12)}/year).\n\n" + "\n".join(lines) +
            f"\n\nTip: review {top['name']} first, since it is your biggest recurring cost. "
            "Cancel anything you haven't used in the last 30 days."
        )

    if has("goal", "target", "emergency fund"):
        goals = ctx.get("goals", [])
        if not goals:
            return intro + "You haven't set a goal yet. Pick one target (for example a 3-month emergency fund) and a deadline, and I'll work out the monthly amount."
        lines = []
        for g in goals[:5]:
            need = f", needs about {_m(ctx, g['monthly_needed'])}/month" if g.get("monthly_needed") else ""
            lines.append(f"- {g['title']}: {g['pct']:.0f}% done ({_m(ctx, g['saved_amount'])} of {_m(ctx, g['target_amount'])}){need}")
        return intro + "Here is where your goals stand:\n\n" + "\n".join(lines) + "\n\nAutomate a transfer on payday so goal money moves before you can spend it."

    if has("budget", "limit", "overspend", "50/30/20"):
        budgets = ctx.get("budgets", [])
        income = cur.get("income", 0)
        if budgets:
            hot = [b for b in budgets if b["status"] != "ok"]
            lines = [f"- {b['category']}: {_m(ctx, b['spent'])} of {_m(ctx, b['limit'])} ({b['pct']:.0f}%)" for b in (hot or budgets)[:6]]
            head = "These budgets need attention:" if hot else "All your budgets are on track:"
            return intro + head + "\n\n" + "\n".join(lines)
        if income:
            return (
                intro + f"With {_m(ctx, income)} income this month, a 50/30/20 starting point is:\n\n"
                f"- Needs (rent, bills, groceries): {_m(ctx, income * .5)}\n"
                f"- Wants (dining, shopping, fun): {_m(ctx, income * .3)}\n"
                f"- Savings and debt: {_m(ctx, income * .2)}\n\nOpen the Budget Planner to set limits per category."
            )
        return intro + "Add your income and a few expenses first, then I can suggest category budgets."

    if has("save", "saving", "invest"):
        rate = cur.get("savings_rate")
        cats = ctx.get("top_categories", [])
        base = f"Your savings rate this month is {rate:.0f}%. " if rate is not None else ""
        tip = f"Your biggest category is {cats[0]['category']} at {_m(ctx, cats[0]['total'])}; trimming it by 10% frees about {_m(ctx, cats[0]['total'] * .1)}." if cats else ""
        return intro + base + tip + "\n\nA solid habit: move 20% of income to savings on payday, then budget with what is left."

    if has("report", "summary", "summar", "month"):
        return intro + _summary_lines(ctx)

    if has("spend", "spent", "expens", "where", "categor", "money go", "cut"):
        cats = ctx.get("top_categories", [])
        if not cats:
            return intro + "I don't see expenses for this month yet. Add a few and I'll break them down."
        lines = [f"- {c['category']}: {_m(ctx, c['total'])} ({c['pct']:.0f}%)" for c in cats[:5]]
        return (
            intro + f"You've spent {_m(ctx, cur.get('expenses', 0))} in {ctx.get('month_label', 'this month')}. Top categories:\n\n"
            + "\n".join(lines) + f"\n\nStart with {cats[0]['category']}: small cuts there move your total the most."
        )

    return intro + _summary_lines(ctx) + "\n\nAsk me about spending, budgets, subscriptions or goals for more detail."


def _summary_lines(ctx):
    cur = ctx.get("current", {})
    rate = cur.get("savings_rate")
    out = [
        f"{ctx.get('month_label', 'This month')}: income {_m(ctx, cur.get('income', 0))}, "
        f"expenses {_m(ctx, cur.get('expenses', 0))}, net {_m(ctx, cur.get('net', 0))}."
    ]
    if rate is not None:
        out.append(f"Savings rate: {rate:.0f}%.")
    chg = cur.get("expense_change_pct")
    if chg is not None:
        out.append(f"Spending is {'up' if chg > 0 else 'down'} {abs(chg):.0f}% vs last month.")
    return " ".join(out)


def offline_report(ctx, label):
    cur = ctx.get("current", {})
    cats = ctx.get("top_categories", [])
    budgets = ctx.get("budgets", [])
    subs = ctx.get("subscriptions", {})
    goals = ctx.get("goals", [])
    lines = [f"I'm PayPilot AI. Here is your report for {label} (generated from your data, without the Gemini model).", "", "## Overview", _summary_lines(ctx), ""]
    lines += ["## Category insights"]
    lines += [f"- {c['category']}: {_m(ctx, c['total'])} ({c['pct']:.0f}%)" for c in cats[:5]] or ["- No expenses recorded."]
    lines += ["", "## Budget check"]
    lines += [f"- {b['category']}: {b['pct']:.0f}% used ({b['status']})" for b in budgets] or ["- No budgets set yet."]
    lines += ["", "## Subscriptions"]
    if subs.get("items"):
        lines.append(f"- {len(subs['items'])} active, about {_m(ctx, subs['monthly_total'])}/month.")
    else:
        lines.append("- None tracked.")
    lines += ["", "## Goals"]
    lines += [f"- {g['title']}: {g['pct']:.0f}% complete" for g in goals] or ["- No goals yet."]
    lines += ["", "## 30-day action plan"]
    steps = []
    over = [b for b in budgets if b["status"] == "over"]
    if over:
        steps.append(f"Bring {over[0]['category']} back under its {_m(ctx, over[0]['limit'])} limit.")
    if cats:
        steps.append(f"Trim {cats[0]['category']} by 10% (about {_m(ctx, cats[0]['total'] * .1)}).")
    if subs.get("items"):
        steps.append("Cancel one subscription you haven't used in 30 days.")
    if goals:
        steps.append(f"Set an automatic transfer toward \"{goals[0]['title']}\" on payday.")
    steps = steps or ["Log every expense for the next 30 days to build a reliable baseline."]
    lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    return "\n".join(lines)
