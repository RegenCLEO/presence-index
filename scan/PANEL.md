# Build a panel

A panel is the file that fixes what a public scan asks: 60 questions, 100 keywords, the engines, the days and the way brands enter. This guide takes you from a category name to a panel that is ready to freeze. The steps after the freeze are in the [README](README.md).

A person owns every panel. An AI assistant can draft the questions and the keyword candidates with the prompts below. A named person then reads every line, edits it and approves it. The published ranking says who approved the panel and whether an AI drafted it.

## What you need

- A folder outside this repository for the draft and its working files.
- A copy of `scan/panel.example.yaml` in that folder, named for the scan.
- A DataForSEO login in `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`. Step 5 makes one paid call with it. No other step in this guide costs money.
- The name of the person who approves the panel.

## Step 1. Fix the category, the market and the dates

- **Category.** A group of businesses a buyer chooses between, named the way a buyer would name it. "Black Friday retailers" is a category. "Retail" is too wide to ask 60 useful questions about.
- **Market.** One country and one language.
- **Scan day and reserve day.** Each question is asked on the scan day. List a reserve day too, because days are part of the hash and cannot be added after the freeze.
- **Keyword pulls.** Two dates in two different calendar weeks, Monday to Sunday. Organic Search is not measured without both. Work back from the publication date: the second pull must finish before the ranking is written.
- **Panel id.** `<category>-<country>-<language>-<year>-<month>`, for example `black-friday-retailers-us-en-2026-11`.

## Step 2. Split the category into groups

The method fixes the number of questions at 60. This guide splits them into 12 general questions and six questions for each of eight groups. A group is a kind of product or service buyers ask about in the category. For Black Friday retailers the groups were TVs, laptops, phones, kitchen appliances, toys, clothing, furniture and video games.

The split stops one product from deciding the ranking. Pick groups that cover what the category sells. Do not pick groups to suit a business you expect to rank.

## Step 3. Write the questions

Rules:

1. Each question is one a buyer in the market would type to an AI assistant when choosing where to buy.
2. The natural answer names businesses in the category. "When does Black Friday start?" gets a date for an answer, names no business and wastes one of the 60 places.
3. No question names a business or a product maker. This is what "unbranded" means in the method.
4. Every group gets the same six kinds of question, so the groups can be compared. The Black Friday panel used the best place to buy, the best deals, the best website, the lowest price, a service question (delivery, returns or price match) and a narrower product inside the group. Another category may need other kinds. Use the same set in every group.
5. No two questions ask the same thing in other words.
6. Each question has an id of lower-case letters and digits, with a short prefix for its group, and its group as `stratum`.

Prompt for an AI assistant. Fill in the three lines at the top.

```
Write 60 questions for a CLEO Presence Index public scan panel.

Category: <the category, as a buyer would name it>
Market: <country>, <language>
Groups: general, and these eight: <group 1>, <group 2>, ... <group 8>

Rules:
1. Each question is one a buyer in this market would type to an AI assistant when
   choosing where to buy.
2. The natural answer to each question names businesses in the category. Leave out
   any question whose answer is advice, a date or a product.
3. Name no business and no product maker in any question.
4. Write 12 general questions. Write six for each group, one of each kind: the best
   place to buy, the best deals, the best website, the lowest price, a service
   question (delivery, returns, price match or support), and a narrower product
   inside the group.
5. No two questions may ask the same thing in other words.
6. Write whole sentences in plain words. End each with a question mark.

Reply with YAML lines only, in this form, with a short id prefix for each group:
  - {id: g01, stratum: general, text: "..."}
```

Then read all 60 yourself. Strike a question that names a business or a maker, that repeats another, or that an engine would answer without naming a business. Paste the result under `questions:` in the panel.

## Step 4. Write the keyword candidates

A keyword is what a person types into Google. It is a short phrase in lower case, not a question. The panel takes 100. Write at least 140 candidates, because some have no search volume and step 5 removes others.

Rules:

1. No keyword names a business or a product maker.
2. The candidates use the groups from step 2. Write about twice as many general candidates as for one group. The Black Friday panel had 28 general candidates and 14 for each group.
3. Each candidate is a phrase a buyer would search for when choosing where to buy. A phrase people search for news or opening hours can stay if it has volume and is about the category.
4. Each candidate has 10 words and 80 characters at most. Google Ads refuses longer ones.

The candidates file is plain text. A `[name]` line starts a group and a `#` line is a comment.

```
# Keyword candidates for Black Friday retailers, United States.
[general]
black friday deals
best black friday deals
[tvs]
black friday tv deals
```

Prompt for an AI assistant:

```
Write keyword candidates for a CLEO Presence Index public scan panel.

Category: <the category, as a buyer would name it>
Market: <country>, <language>
Groups: general, and these eight: <group 1>, <group 2>, ... <group 8>

Rules:
1. Each keyword is a short phrase a buyer in this market would type into Google when
   choosing where to buy. It is not a question to an AI assistant.
2. Name no business and no product maker in any keyword.
3. Write 28 keywords for general and 14 for each group.
4. Lower case. 10 words and 80 characters at most.
5. No keyword may differ from another only by a plural or by word order.

Reply with plain text only: a [group name] line, then one keyword on each line.
```

## Step 5. Look up the volumes and fill the panel

```
python -m scan volumes --panel DRAFT.yaml --out BUILD_DIR --candidates CANDIDATES.txt
python -m scan volumes --panel DRAFT.yaml --out BUILD_DIR --candidates CANDIDATES.txt --go
```

The first command makes no call. It says what the second one would send. The second makes one call to DataForSEO for Google Ads monthly search volumes in the panel's country. DataForSEO charged USD 0.09 for this call in November 2026.

The command then keeps the 100 candidates with the highest volume and writes them, with their volumes, over the `keywords:` block of the draft. Ties sort by keyword text. It refuses a frozen panel. `BUILD_DIR` is a folder for this step's files and is not the scan's output directory.

Two rules remove a candidate before the cut:

1. **Excluded words.** `--exclude-word WORD` removes every candidate that contains the word. Use it for a neighbouring subject the category does not cover. The Black Friday panel used `--exclude-word cyber` to leave out Cyber Monday keywords. Repeat the option for more words.
2. **Near duplicates.** A candidate that differs from another only by a plural "s" or by word order is removed. Google reports one combined volume for such a pair, so keeping both would count it twice. The one with the higher volume stays.

Files in `BUILD_DIR`:

| File | Holds |
|---|---|
| `volume-lookup.json` | the request and DataForSEO's reply as it arrived |
| `keyword-volumes.csv` | every candidate with its group, its volume, whether it was kept and why it was removed |
| `cost_log.jsonl` | one row for the paid call |

A rerun with the same candidates makes no call. It ranks the saved reply again, so a new `--exclude-word` or `--keep` costs nothing. Changed candidates need a new call with `--go`.

Read the two lines the command prints at the end:

- **Lowest kept and highest dropped volume.** When they are equal, the alphabet decided the last place. Say so in the panel notes.
- **The share of the largest keyword.** Organic Search weights each keyword by its volume, so one large keyword can carry most of the score. In the Black Friday panel "black friday deals" carried 51.7%. Publish this share with the ranking. Do not remove a keyword to lower it.

If fewer than 100 candidates have a volume, the command writes the table, leaves the panel unchanged and stops. Add candidates and run it again with `--go`.

## Step 6. Decide how brands enter

The panel has two modes.

- **`list_plus_ai`.** The panel lists the brands from a named outside source, in that source's order. Write the source and any rule for skipping its entries into the panel as comments. The scan can add up to `ai_named_max` businesses the answers name.
- **`ai_top`.** The panel lists no brand. The scan ranks the `ai_named_max` businesses the answers name most often, after a person confirms each one's domain.

Appendix D of the method describes a panel that lists its brands from a named outside source. A scan in `ai_top` mode departs from that wording, so the owner of the method must agree to it before the freeze.

For either mode, write the inclusion rule before the scan runs: which businesses the answers name count as members of the category. For retailers, the rule has to say whether a deal site, a marketplace seller, a phone carrier and a maker's own store count. A rule written after the answers are in can be bent toward a result.

## Step 7. Fill in the engines and the readers

`panel.example.yaml` lists each engine with the key it needs. Name a model for every engine and for the three answer readers. No engine has a default model. A scan uses one ChatGPT route for all its ChatGPT answers. The [README](README.md) explains the two routes.

## Step 8. Get the panel approved, then freeze it

The approver reads all 60 questions and all 100 keywords against the rules above, and checks the dates, the brand mode and the inclusion rule. Record the approver's name and the date in a comment at the top of the panel. If an AI drafted the questions or the candidates, record which model and on what date.

```
python -m scan freeze PANEL.yaml
```

Nothing in the panel changes after the freeze. A changed panel is a new panel with a new id and a new output directory. Rehearse on a frozen copy of the panel in its own output directory, as the README describes.

## What to keep for publication

Keep these with the scan's records. Together they show a reader how the panel was made.

- The candidates file, `keyword-volumes.csv` and `volume-lookup.json`.
- The prompts as you sent them, with the model and the date.
- The inclusion rule and the brand source, if there is one.
- The approver's name and the date of approval.
