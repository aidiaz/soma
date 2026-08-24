---
name: weekly-review
description: Run the weekly training review — pull the merged week from traindb and decide what should change. Use when asked "how was my week", "what should I change", "review my training", or at the start of a new training week. Also use when asked whether to add or cut load, or whether a fatigue pattern warrants a rest day.
---

# Weekly training review

The database has already done the join. Call `get_training_week` once and reason
over what comes back — do not fetch the pieces separately and correlate them
here.

## Procedure

1. **`get_training_week()`** for the week in question. Everything below reads
   from its `signals` and `coverage` blocks.
2. **Check `coverage` first.** A week with three days of health data and two of
   food is not a week of bad numbers, it is a week of missing ones. Say so
   plainly and do not apply the rules below to averages built from two days.
   Missing food days in particular are unknown intake, never a fast.
3. **Apply the rules in order.** Intake first, always.
4. **Give one recommendation**, not a list of observations. The output of this
   review is what changes next week.

## Rules

Thresholds live here, not in the database. The tools return measurements; this
file decides what they mean, and it is version-controlled so a revision is a
diff rather than a lost conversation.

### Fuel — check before anything else

| Signal | Threshold | Response |
|---|---|---|
| `avg_kcal` | under 1,750 | **Flag first, before anything else.** This is the historic failure mode. Nothing else in this review matters until it is addressed. |
| `avg_protein_g` | under 126 | Flag |

### Recovery

| Signal | Threshold | Response |
|---|---|---|
| `resting_hr_delta` with `hrv_delta` | RHR up 5+ bpm and HRV falling | Cut weekly TSS by 30%, add a rest day |

Use `get_health_trend(days=14)` to confirm a three-day pattern before acting —
`resting_hr_3d_avg` against `resting_hr_baseline_avg`. One bad night is not a
trend, and the weekly average can hide the shape.

### Body composition

| Signal | Threshold | Response |
|---|---|---|
| `weight_change_kg` | falling faster than 1 kg/week | Add 200 kcal/day. Muscle is leaving. |
| `waist_cm` | flat 3+ weeks | Re-weigh food for a week before cutting anything |

Waist is the primary measure for the first four weeks. Weight alone moves with
water and glycogen and will mislead over a single week.

### Load

| Signal | Threshold | Response |
|---|---|---|
| `tss_ramp_pct` | over +10% | Hold the week, do not build |
| `sessions_completed` | 2+ short of plan | Repeat the week, do not skip ahead |
| all sessions done, ramp under 10%, felt good | — | Add 10 min to the long ride |

`tss_ramp_pct` of `null` means the prior week carried no load. That is a first
week back, not infinite growth — the ramp rule does not apply.

## The framing rule

**When in doubt, the likely error is eating too little, not training too little.**

If the week looks flat, the reflex to add training is usually wrong. Check
intake before load, every time.

## A caution on the units

`tss` currently holds Garmin's EPOC-derived training load, not true TSS. The
ramp is a ratio, so it survives a consistent scale. Absolute TSS numbers should
not be compared against published training plans until Wahoo ingestion lands,
and a week that straddles the switchover will produce a ramp that is an artefact
of the change rather than of the training.
