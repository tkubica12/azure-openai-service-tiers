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
