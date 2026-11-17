# Sumo Predictor

Machine-learning predictions for Makuuchi sumo bouts.  
Live site: **https://datwins.github.io/sumo_prediction**

## How it works

| Step | Script | When |
|------|--------|------|
| 1. Scrape data | `sumo_scraper.py` | Once, then after each basho |
| 2. Train model | `sumo_model.py` | After scraping new data |
| 3. Predict today | `sumo_predict_json.py` | Each day during a basho |
| 4. Push to site | `git push` | After generating predictions |

## Setup (Windows)

```bash
pip install requests lightgbm pandas numpy joblib scikit-learn
```

## Workflow

```bash
# First time — scrape all bouts 2000–present (takes 30–60 min)
python sumo_scraper.py

# Train the model
python sumo_model.py

# Each day of a basho — predict today's card
python scripts/sumo_predict_json.py 202411 10

# Copy predictions.json to docs/ and push
copy predictions.json docs\predictions.json
git add docs/predictions.json
git commit -m "Day 10 predictions"
git push
```

## H2H lookup (run once per basho or when you want to refresh)

```bash
python scripts/sumo_predict_json.py 202411 10 --h2h
copy h2h.json docs\h2h.json
git add docs/h2h.json
git commit -m "Update H2H lookup"
git push
```

## Model features

- **H2H win %** — career head-to-head record vs this opponent (most important)
- **Record diff** — current basho win-loss difference
- **Rank diff** — rank gap in integer form (Yokozuna=1, M16=36)
- **Momentum** — wins in last 3 bouts this basho
- **KK safe / MK risk** — kachi-koshi (8+ wins) or make-koshi (7+ losses) pressure
- **Oshi ratio** — % of career wins by pushing technique
- **BMI diff** — body mass difference
- **Age diff** — age gap on day of bout
- **Day** — day of basho

Accuracy: **75.3%** on 2024+ holdout (trained on 2000-2023, ~47k Makuuchi bouts).

## GitHub Pages setup

In your repo Settings → Pages → Source → `docs/` folder.  
The site will be live at `https://datwins.github.io/sumo_prediction`.
