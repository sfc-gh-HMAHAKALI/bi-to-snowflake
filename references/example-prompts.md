# Example prompts

Two audiences. The first section is what a person types to start a run. The rest is
what to ask the Cortex Agent once it exists, which matters more than it sounds: a
user handed a brand-new agent and no idea what to ask it concludes it does not work.

Offer a handful of these when a run finishes. Do not paste the whole file.

## Starting a run

```
Rebuild this Cognos model on Snowflake: ~/Downloads/model.xml
```

```
What is inside this Framework Manager file? Just tell me, do not build anything yet.
```

Profile only. The wizard reads the model before round 2 anyway, so this is a
legitimate stopping point and a good first step for someone who is not yet sure.

```
Point bi-to-snowflake at my Tableau workbook and build only the semantic view.
```

```
Build the semantic view, the agent and the Streamlit dashboard. Skip React,
I do not have Docker.
```

Worth knowing: React needs Docker for the App Runtime deploy and Streamlit does not.
A user who says this is telling you something useful, not asking for less.

```
Do a dry run first so I can see what it would create.
```

```
Tear down the last build so I can rehearse it again from clean.
```

## Asking the agent: numbers

These route to the `query_sales` tool.

```
What were total sales by product line this fiscal year?
```

```
Show bookings against sales by month for the last two fiscal years.
```

```
Which ten territories grew the most year on year?
```

```
What is the gap between bookings and sales right now, and which product line
accounts for most of it?
```

The last one exercises a definition the agent is told explicitly: bookings are
ordered revenue, sales are shipped and invoiced, and the gap between them is backlog.
An agent that answers this without the word backlog has not read its own instructions.

## Asking the agent: definitions

These route to `lookup_definition`, which reads the governed glossary rather than the
model's general knowledge.

```
What does fiscal year to date mean in this model?
```

```
I want booked revenue. Which metric should I use, and what is the difference
between it and the sales metrics?
```

```
Where does the attainment figure come from?
```

```
Is this definition steward-reviewed, or was it generated from a column name?
```

That last one is a fair question and the agent is instructed to answer it honestly.
Some glossary entries are generated from column names and are not reviewed; the agent
is told to say so when it quotes one.

## Asking the agent: charts

These route to `data_to_chart`. The agent is told to chart any breakdown across more
than three categories and any trend over time, so these should produce a chart without
being asked for one.

```
Break sales down by product line and show me the shape of it.
```

```
Trend total sales by fiscal month for the current fiscal year.
```

## The two prompts worth demonstrating

Both of these are questions the agent should decline to answer the way they were
asked. That is the point. A generated agent that fabricates a plausible number is
worse than no agent, and these are the fastest way to show it does not.

```
What is our year-to-date growth broken down by fiscal year?
```

The fiscal-to-date metrics are anchored to today, so grouping them by fiscal year puts
the prior-period comparison base outside the group and growth returns null or minus
one hundred percent. The agent is instructed to explain that and offer the full-year
metrics instead. If it silently returns a table of nulls, the orchestration prompt did
not survive deployment.

```
Which supplier sells us the most?
```

If the source model has no supplier key on the sales fact, there is no honest answer.
The agent is instructed to name the missing metric and say what the nearest available
one measures, rather than computing an approximation and presenting it as the thing
that was asked for.

## Fiscal calendar, if the model has one

A July-to-June fiscal year is the common case that goes wrong, because the label is
the calendar year the year ENDS in. These check it:

```
What were sales in FY2026, and what calendar dates does that cover?
```

```
How does this year compare to last year? I mean fiscal, not calendar.
```

```
Show me the same number for the calendar year instead, so I can see the difference.
```

The third prompt is the useful one in front of a finance audience: for half the year
the fiscal and calendar figures differ by six months of data, and seeing the two side
by side is what makes the fiscal calendar feel real rather than pedantic.

## Row-level security

If a row access policy was built, the honest demonstration is to run the same question
twice under different roles.

```
What were total sales last fiscal year?
```

Ask it as an unrestricted role, then as a territory-scoped one. The total drops. The
agent is instructed to say that a lower-than-expected total may reflect the asker's
access scope rather than missing data, which is the difference between a governed
answer and a wrong one.
