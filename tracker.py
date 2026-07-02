"""
Fitness & Nutrition Tracker — Streamlit app (Teal/Turquoise theme), backed by Supabase.

Architecture
------------
- Supabase (Postgres + PostgREST) is the single source of truth, accessed via
  the `supabase-py` client. The client is built from `st.secrets["SUPABASE_URL"]`
  / `st.secrets["SUPABASE_KEY"]` and cached with `st.cache_resource` so it's
  created once per app process rather than on every rerun.
- This app targets the tables as they already exist in the connected Supabase
  project (introspected via the PostgREST OpenAPI schema, not assumed):
    - workouts(id, date, workout_type)
    - exercises(id, workout_id -> workouts.id, exercise_name, reps, weight, note)
    - nutrition(id, date, protein_consumed, creatine_taken [boolean])
    - settings(id, key, value) — a generic key/value store; body weight is
      the row where key = 'body_weight', value stored as text.
  The Supabase REST client has no DDL support, so this file does not create
  or alter tables.
- `exercises` has no explicit set-number column. This app always inserts a
  workout's sets in order, so "which set was this" is inferred from
  insertion order (ascending `id`) within a given workout_id + exercise_name,
  rather than stored explicitly. See `get_previous_sets()`.
- `nutrition` has a surrogate `id` primary key (not `date`), so "one row per
  day" is enforced in application code (select-then-insert-or-update) rather
  than via upsert-on-conflict.
- All "today's inputs" for the workout form live in `st.session_state`, keyed
  by exercise index and set number, so they can be cleared after saving
  without needing a page reload.
- Color theme (primaryColor etc.) is set in `.streamlit/config.toml`, which
  covers native accents (progress bar, focus rings). CSS injected below
  layers RTL support plus the turquoise button/header styling on top.
"""

from datetime import date, datetime

import streamlit as st
from supabase import create_client, Client

PRIMARY_COLOR = "#20B2AA"
DARK_TEAL = "#008080"
BACKGROUND_COLOR = "#F8FFFF"
SETTINGS_WEIGHT_KEY = "body_weight"

EXERCISES = [
    {"name": "סקוואט במוט חופשי", "cue": None},
    {"name": "יד קדמית בכיסא כומר (מכונה)", "cue": None},
    {"name": "אמה בפולי תחתון", "cue": None},
    {"name": "פשיטת ברכיים במכונה", "cue": None},
    {"name": "תאומים בסמית משין", "cue": "בכל סט לשנות את מנח הרגליים לכיוון אחר."},
    {"name": "תאומים בישיבה", "cue": "להמתין שנייה בירידה ו-2 שניות במתיחה בעלייה."},
    {"name": "תרגיל גב בפולי עליון", "cue": "עבודה מהשכמות, מרפקים לכיסים ולהבליט חזה."},
]
SETS_PER_EXERCISE = 3
CREATINE_REMINDER_START_HOUR = 6


# --------------------------------------------------------------------------
# Database layer (Supabase)
# --------------------------------------------------------------------------

@st.cache_resource
def get_client() -> Client:
    """One Supabase client shared across reruns/sessions of this app."""
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


def get_body_weight(supabase: Client) -> float:
    response = supabase.table("settings").select("value").eq("key", SETTINGS_WEIGHT_KEY).execute()
    return float(response.data[0]["value"]) if response.data else 66.0


def set_body_weight(supabase: Client, weight: float) -> None:
    supabase.table("settings").update({"value": str(weight)}).eq("key", SETTINGS_WEIGHT_KEY).execute()


def get_nutrition_today(supabase: Client) -> dict | None:
    today = date.today().isoformat()
    response = supabase.table("nutrition").select("*").eq("date", today).execute()
    return response.data[0] if response.data else None


def adjust_protein(supabase: Client, delta: float) -> None:
    """Adds `delta` grams of protein to today's row, floored at 0. `nutrition`
    has no unique constraint on `date`, so this selects first and then either
    updates the existing row (by id) or inserts a new one, rather than
    relying on upsert-on-conflict."""
    today = date.today().isoformat()
    row = get_nutrition_today(supabase)
    if row:
        new_protein = max(0.0, row["protein_consumed"] + delta)
        supabase.table("nutrition").update({"protein_consumed": new_protein}).eq("id", row["id"]).execute()
    else:
        new_protein = max(0.0, delta)
        supabase.table("nutrition").insert(
            {"date": today, "protein_consumed": new_protein, "creatine_taken": False}
        ).execute()


def log_creatine_today(supabase: Client) -> None:
    """Marks creatine as taken today, preserving today's protein total."""
    today = date.today().isoformat()
    row = get_nutrition_today(supabase)
    if row:
        supabase.table("nutrition").update({"creatine_taken": True}).eq("id", row["id"]).execute()
    else:
        supabase.table("nutrition").insert(
            {"date": today, "protein_consumed": 0, "creatine_taken": True}
        ).execute()


def get_previous_sets(supabase: Client, exercise_name: str) -> list[dict]:
    """
    Returns [{"reps": ..., "weight": ...}, ...] for the most recent workout
    that included this exercise, in the order those sets were logged. Since
    `exercises` has no set-number column, set order is inferred from
    insertion order (ascending `id`) within that one workout_id — this app
    always inserts a workout's sets in order, so that's a reliable proxy for
    "set 1, set 2, set 3".
    """
    latest = (
        supabase.table("exercises")
        .select("workout_id")
        .eq("exercise_name", exercise_name)
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    if not latest.data:
        return []
    workout_id = latest.data[0]["workout_id"]
    rows = (
        supabase.table("exercises")
        .select("reps, weight")
        .eq("exercise_name", exercise_name)
        .eq("workout_id", workout_id)
        .order("id")
        .execute()
    )
    return rows.data


def save_workout_session(supabase: Client) -> int:
    """
    Reads today's set inputs out of session_state, saves a new workout + its
    completed sets, and returns how many set rows were saved (0 = nothing to
    save, so the caller can skip creating an empty workout row).
    """
    rows_to_insert = []
    for idx, exercise in enumerate(EXERCISES):
        notes = st.session_state.get(f"notes_{idx}", "")
        for set_number in range(1, SETS_PER_EXERCISE + 1):
            reps = st.session_state.get(f"reps_{idx}_{set_number}", 0)
            weight = st.session_state.get(f"weight_{idx}_{set_number}", 0.0)
            if reps > 0 or weight > 0:
                rows_to_insert.append(
                    {
                        "exercise_name": exercise["name"],
                        "reps": reps,
                        "weight": weight,
                        "note": notes,
                    }
                )

    if not rows_to_insert:
        return 0

    workout_response = (
        supabase.table("workouts")
        .insert({"date": date.today().isoformat(), "workout_type": "עומס פרוגרסיבי"})
        .execute()
    )
    workout_id = workout_response.data[0]["id"]

    for row in rows_to_insert:
        row["workout_id"] = workout_id
    supabase.table("exercises").insert(rows_to_insert).execute()

    return len(rows_to_insert)


def clear_workout_inputs() -> None:
    """Removes today's set/notes keys from session_state so widgets reset on rerun."""
    for idx in range(len(EXERCISES)):
        st.session_state.pop(f"notes_{idx}", None)
        for set_number in range(1, SETS_PER_EXERCISE + 1):
            st.session_state.pop(f"reps_{idx}_{set_number}", None)
            st.session_state.pop(f"weight_{idx}_{set_number}", None)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def inject_custom_css() -> None:
    """
    RTL layout support plus the turquoise/teal visual identity: rounded bold
    buttons, dark-teal headers, and a turquoise progress bar. `.streamlit/
    config.toml` sets the base theme (primaryColor etc.); this CSS layers
    the RTL flip and button/header styling that the theme alone can't do.
    """
    st.markdown(
        f"""
        <style>
            .stApp {{ direction: rtl; text-align: right; background-color: {BACKGROUND_COLOR}; }}
            .stApp [data-testid="stMarkdownContainer"],
            .stApp [data-testid="stCaptionContainer"] {{ text-align: right; }}
            .stApp label {{ text-align: right !important; width: 100%; }}
            .stApp [data-testid="stNumberInput"] input,
            .stApp [data-testid="stTextInput"] input {{ text-align: right; }}
            .stApp [data-testid="stExpander"] {{ direction: rtl; text-align: right; }}
            .stApp [data-testid="stMetricLabel"] {{ justify-content: flex-end; }}

            h1, h2, h3, h4, h5 {{ color: {DARK_TEAL} !important; }}

            .stButton > button {{
                background-color: {PRIMARY_COLOR};
                color: white;
                font-weight: 700;
                border-radius: 999px;
                border: none;
                padding: 0.5em 1.2em;
                transition: background-color 0.15s ease-in-out;
            }}
            .stButton > button:hover {{
                background-color: {DARK_TEAL};
                color: white;
            }}

            div[data-testid="stProgress"] > div > div > div > div {{
                background-color: {PRIMARY_COLOR};
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_settings_and_nutrition(supabase: Client) -> None:
    st.header("🥩 תזונה ותוספים")

    current_weight = get_body_weight(supabase)
    weight_input = st.number_input(
        "משקל גוף נוכחי (ק\"ג)", min_value=0.0, step=0.5, value=float(current_weight), key="weight_input"
    )
    if weight_input != current_weight:
        set_body_weight(supabase, weight_input)
        current_weight = weight_input

    protein_goal = current_weight * 2

    nutrition_today = get_nutrition_today(supabase)
    creatine_taken = bool(nutrition_today and nutrition_today["creatine_taken"])
    if datetime.now().hour >= CREATINE_REMINDER_START_HOUR and not creatine_taken:
        st.warning("⚠️ נא לאשר צריכת מנת קריאטין יומית!")
        if st.button("סימון שלקחתי קריאטין היום"):
            log_creatine_today(supabase)
            st.rerun()

    protein_today = nutrition_today["protein_consumed"] if nutrition_today else 0.0
    st.subheader("מעקב חלבון יומי")
    st.write(f"{protein_today:.0f} / {protein_goal:.0f} גרם")
    progress_ratio = min(protein_today / protein_goal, 1.0) if protein_goal > 0 else 0.0
    st.progress(progress_ratio)

    amount = st.number_input("כמות לעדכון (גרם)", min_value=0, value=10, step=5, key="protein_amount")
    col_add, col_sub = st.columns(2)
    with col_add:
        if st.button("הוסף חלבון ➕", use_container_width=True):
            adjust_protein(supabase, amount)
            st.rerun()
    with col_sub:
        if st.button("תקן חלבון ➖", use_container_width=True):
            adjust_protein(supabase, -amount)
            st.rerun()


def render_workouts(supabase: Client) -> None:
    st.header("🏋️ אימון – עומס פרוגרסיבי")

    for idx, exercise in enumerate(EXERCISES):
        with st.expander(f"{idx + 1}. {exercise['name']}", expanded=False):
            if exercise["cue"]:
                st.caption(f"💡 {exercise['cue']}")

            prev_sets = get_previous_sets(supabase, exercise["name"])
            for set_number in range(1, SETS_PER_EXERCISE + 1):
                prev = prev_sets[set_number - 1] if len(prev_sets) >= set_number else None
                if prev:
                    st.info(f'אימון קודם: {prev["reps"]} חזרות | {prev["weight"]} ק"ג')
                else:
                    st.info("-")

                col_reps, col_weight = st.columns(2)
                col_reps.number_input(
                    "חזרות", min_value=0, step=1, key=f"reps_{idx}_{set_number}"
                )
                col_weight.number_input(
                    "משקל (ק\"ג)", min_value=0.0, step=0.5, key=f"weight_{idx}_{set_number}"
                )

            st.text_input("הערות", key=f"notes_{idx}")

    st.divider()
    if st.button("סיום אימון – שמור נתונים 💾", type="primary", use_container_width=True):
        saved_sets = save_workout_session(supabase)
        if saved_sets:
            clear_workout_inputs()
            st.toast("האימון נשמר בהצלחה! 💾", icon="✅")
            st.success(f"האימון נשמר בהצלחה! נשמרו {saved_sets} סטים.")
            st.rerun()
        else:
            st.warning("לא הוזנו נתונים לשמירה.")


def main() -> None:
    st.set_page_config(page_title="מעקב כושר ותזונה", page_icon="🏋️", layout="wide")
    inject_custom_css()
    st.title("📊 מעקב כושר ותזונה")

    supabase = get_client()

    render_settings_and_nutrition(supabase)
    st.divider()
    render_workouts(supabase)


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------
# Schema reference (as introspected from the live Supabase project — this
# app does not create or alter tables; nothing below is executed):
# --------------------------------------------------------------------------
#
# workouts(id, date, workout_type)
# exercises(id, workout_id -> workouts.id, exercise_name, reps, weight, note)
# nutrition(id, date, protein_consumed, creatine_taken [boolean])
# settings(id, key, value)  -- body weight lives at key='body_weight'
#
# Note: `exercises` has no set-number column, so this app infers set order
# from insertion order within a workout (see get_previous_sets()). If you
# want first-class per-set tracking instead of that inference, you could add:
#     alter table exercises add column set_number int;
# — but that's an optional future improvement, not something this app
# requires or applies automatically.
