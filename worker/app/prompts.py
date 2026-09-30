"""Small, realistic prompts. The demo measures latency and availability, not token throughput,
so prompts are short and outputs are capped."""

PROMPTS = [
    ("summarize", "Summarize in 3 bullet points why asynchronous message queues improve the resilience "
                  "of cloud applications."),
    ("classify", "Classify the sentiment (positive/negative/neutral) of this customer review and justify it "
                 "in one sentence: 'The delivery was two days late, but support refunded shipping right away "
                 "and the product works great.'"),
    ("extract", "Extract the fields name, company, city and requested_date as JSON from this text: "
                "'Hi, this is Petra Novak from Contoso Logistics in Brno. Could we meet on 14 October?'"),
    ("reason", "A container app scales from 0 to 1 replica when a message arrives and back to 0 after 5 "
               "idle minutes. Messages arrive every 20 minutes. Estimate how many cold starts happen per "
               "day and explain briefly."),
    ("write", "Write a 4-sentence product description for a reusable stainless-steel water bottle aimed at "
              "hikers."),
    ("translate", "Translate to Czech and German: 'Batch processing is cheaper but results arrive later.'"),
    ("code", "Write a short Python function that returns the p95 of a list of floats without numpy."),
    ("explain", "Explain to a non-technical manager in 3 sentences what 'latency percentile p90' means."),
]

# --- "heavy" probe: large deterministic input + long, fixed-shape output -------------------------------------
# Same context every run (fixed seed) so input size is constant; a unique nonce at the very START of the prompt
# defeats prompt caching (caching matches on the prefix). The task asks for a fixed number of fixed-shape blocks
# so the output token count stays as stable as possible between runs and tiers.

HEAVY_TICKETS = 50  # ~4.6k input tokens (measured)
HEAVY_ANSWERS = 16  # ~1.15k output tokens (measured ~72 tokens per answer)

_PRODUCTS = ["Contoso Router X2", "Fabrikam Sensor Hub", "Northwind POS Terminal", "Tailspin Drone Kit",
             "Litware Smart Lock", "Adatum Cloud Backup", "Proseware Payroll", "Wingtip Toys App",
             "Woodgrove Banking Portal", "Alpine Ski Rental Kiosk"]
_ISSUES = ["fails to connect after the latest firmware update", "shows an incorrect invoice total",
           "reboots randomly every few hours", "cannot sync data with the mobile app",
           "rejects valid login credentials", "reports battery level incorrectly",
           "exports CSV files with broken characters", "is very slow during peak business hours",
           "sends duplicate email notifications", "loses configuration after a power outage"]
_CONTEXTS = ["The customer has already tried restarting the device twice.",
             "This started right after our IT department changed the proxy settings.",
             "Our warehouse team depends on this every morning before 7am.",
             "We have about 40 users affected across two branch offices.",
             "The problem happens only on Mondays when the weekly report runs.",
             "Support previously suggested a factory reset but that did not help.",
             "We are under a contractual deadline and need a workaround quickly.",
             "Logs attached show timeout errors followed by a retry storm."]
_CITIES = ["Brno", "Prague", "Ostrava", "Plzen", "Olomouc", "Liberec", "Vienna", "Bratislava", "Krakow", "Munich"]
_NAMES = ["Petra", "Jan", "Eva", "Martin", "Lucie", "Tomas", "Jana", "Pavel", "Klara", "David"]


def _heavy_context() -> str:
    import random

    rnd = random.Random(20260930)
    lines = []
    for i in range(1, HEAVY_TICKETS + 1):
        lines.append(
            f"Ticket T{i:03d} | from {rnd.choice(_NAMES)} in {rnd.choice(_CITIES)} | product: {rnd.choice(_PRODUCTS)}\n"
            f"Description: Our {rnd.choice(_PRODUCTS)} {rnd.choice(_ISSUES)}. {rnd.choice(_CONTEXTS)} "
            f"{rnd.choice(_CONTEXTS)} We also noticed that it {rnd.choice(_ISSUES)}. "
            f"Order reference ORD-{rnd.randint(100000, 999999)}, contract tier {rnd.choice(['Basic', 'Plus', 'Premium'])}, "
            f"first reported {rnd.randint(1, 28)} days ago, {rnd.randint(1, 60)} users impacted."
        )
    return "\n\n".join(lines)


HEAVY_CONTEXT = _heavy_context()


def heavy_prompt(nonce: str) -> str:
    return (
        f"Request nonce: {nonce}\n\n"
        "You are a senior support triage engineer. Below is a list of customer support tickets.\n\n"
        f"=== TICKETS ===\n{HEAVY_CONTEXT}\n=== END OF TICKETS ===\n\n"
        f"Task: triage ONLY tickets T001 to T{HEAVY_ANSWERS:03d} (exactly {HEAVY_ANSWERS} tickets, in order). "
        "For each ticket output exactly this block and nothing else:\n"
        "### T<number>\n"
        "Category: <one of Connectivity, Billing, Stability, Sync, Authentication, Hardware, Data, Performance, "
        "Notifications, Configuration>\n"
        "Priority: <P1, P2 or P3>\n"
        "Summary: <exactly 30 words describing the problem and its business impact>\n"
        "Next step: <exactly 15 words describing the concrete next action for the support engineer>\n\n"
        "Do not add any introduction, conclusion or extra text."
    )
