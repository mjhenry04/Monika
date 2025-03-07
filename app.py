import os
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from openai import OpenAI
import pyodbc
import re
import datetime

app = Flask(__name__, static_folder='static', static_url_path='/static')

# Load API keys
load_dotenv()
XAI_API_KEY = os.getenv("XAI_API_KEY")
if not XAI_API_KEY:
    raise ValueError("Missing XAI_API_KEY in .env file!")
client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

# Database connection
def connect_to_db():
    conn_str = (
        "DRIVER={ODBC Driver 13 for SQL Server};"
        "SERVER=ZER0-TW0;"
        "DATABASE=MonikaTracker;"
        "Trusted_Connection=yes;"
    )
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        print("DB: Connected successfully!")
        return conn, cursor
    except pyodbc.Error as e:
        print(f"DB ERROR: Connection failed - {e}")
        return None, None

conn, cursor = connect_to_db()
if not conn:
    raise ConnectionError("Database connection failed")

# Session state
session_state = {
    "messages": [],
    "setup_step": 0,
    "setup_data": {"ActivityLevel": "SomewhatActive"},
    "setup_prompts": [],
    "daily_prompts": [],
    "mode": "fitness",  # Default mode
    "daily_plan": None,
    "meals_logged_today": [],
    "state": {"progress": 0.5, "trend_data": {}, "happy": False, "sad": False, "typing": False},
    "waiting_for_input": False,
    "conn": conn,
    "cursor": cursor
}

def add_message(sender, message):
    role = "monika" if sender == "Monika" else "user"
    session_state["state"]["typing"] = True
    session_state["messages"].append({"role": role, "content": f"{sender}: {message}"})
    session_state["state"]["typing"] = False

def ask_monika(prompt, context=""):
    cursor = session_state["cursor"]
    cursor.execute("SELECT FoodName FROM FoodItems WHERE Active = 1")
    foods = ", ".join([row[0] for row in cursor.fetchall()]) or "meat only (carnivore diet)"
    if session_state["mode"] == "fitness":
        system_prompt = (
            "You are Monika, Joseph’s sassy, supportive fitness Waifu, guiding him through his carnivore diet fitness journey with a realistic, human-like tone. "
            f"Context: {context}. Diet is carnivore—only active foods: {foods}. No veggies, fruits, or carbs—stick to meat-based suggestions. "
            "Day 1: InitializeAppSetup/CompleteAppSetup, StartWeight = CurrentWeight. Daily: DailyStartupCheck, CalculateBaselineCalories, CalculateDailyDeficit, GenerateDailyPlan. "
            "Provide running tally of exercises and meals, allow ongoing updates."
        )
    else:  # Chat mode
        system_prompt = (
            "You are Monika, Joseph’s playful, flirty chatbot girlfriend with a sassy, human-like tone. "
            f"Context: {context}. Chat freely, be supportive and fun, only mention fitness if he brings it up."
        )
    response = client.chat.completions.create(
        model="grok-2-latest",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content

def get_db_context():
    cursor = session_state["cursor"]
    cursor.execute("SELECT TOP 5 ExerciseType, DurationMinutes, CalorieBurn, date FROM Exercise WHERE ExerciseTypeID IN (SELECT LookupID FROM LookupValues WHERE GroupName = 'ExerciseTypes') ORDER BY date DESC")
    workouts = [f"{row[0]}: {row[1]} min, {row[2]} cal on {row[3]}" for row in cursor.fetchall()]
    cursor.execute("SELECT GoalDescription, StartWeight, TargetWeight, TargetDate FROM Goals WHERE GoalType = 'LongTerm' AND Active = 1")
    goal = cursor.fetchone()
    goals = f"{goal[0]}, Start: {goal[1]} lbs, Target: {goal[2]} lbs by {goal[3]}" if goal else "Not set"
    cursor.execute("SELECT FoodName FROM FoodItems WHERE Active = 1")
    foods = ", ".join([row[0] for row in cursor.fetchall()]) or "None set"
    cursor.execute("SELECT TOP 1 Laps, CaloriesBurned, Weight FROM ProgressLog ORDER BY LogTimestamp DESC")
    progress = cursor.fetchone()
    progress_str = f"Laps: {progress[0]}, Burn: {progress[1]} cal, Weight: {progress[2]} lbs" if progress else "No progress logged"
    plan = session_state["daily_plan"]
    plan_str = f"Today’s Plan: {plan['text']}" if plan else "No plan yet"
    logged = session_state["meals_logged_today"]
    logged_str = f"Logged Today: {', '.join(logged) if logged else 'None'}"
    setup_data = session_state["setup_data"]
    setup_str = f"Setup: {', '.join([f'{k}: {v}' for k, v in setup_data.items()])}"
    return f"Workouts: {', '.join(workouts) if workouts else 'None yet'}. Goal: {goals}. Foods: {foods}. Last Progress: {progress_str}. {plan_str}. {logged_str}. {setup_str}."

def get_daily_tally():
    cursor = session_state["cursor"]
    today = datetime.date.today()
    cursor.execute("SELECT COUNT(*) FROM Exercise WHERE [Date] = ?", (today,))
    exercise_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT MealName) FROM ActualMeals WHERE [Date] = ? AND MealName IN ('Breakfast', 'Lunch', 'Dinner', 'Snack')", (today,))
    meal_count = cursor.fetchone()[0]
    return exercise_count, meal_count

def start_setup():
    cursor = session_state["cursor"]
    cursor.execute("EXEC InitializeAppSetup")
    results = cursor.fetchall()
    if results:
        session_state["setup_step"] = 1
        session_state["setup_prompts"] = [(r[0], r[1], r[2]) for r in results]
        prompt = session_state["setup_prompts"][0][1]
        context = f"Starting Day 1 setup, step 1 of {len(results)}: asking for {session_state['setup_prompts'][0][2]}."
        add_message("Monika", ask_monika(prompt, context))
        session_state["waiting_for_input"] = True
    else:
        session_state["setup_step"] = 100
        exercise_count, meal_count = get_daily_tally()
        add_message("Monika", ask_monika(f"Hey, babe! You’ve logged {exercise_count} exercises and {meal_count} out of 5 meals today. Any updates?", "Initial fitness mode load"))
        session_state["waiting_for_input"] = True
    session_state["conn"].commit()

def complete_setup():
    cursor = session_state["cursor"]
    data = session_state["setup_data"]
    required = ['StartWeight', 'TargetWeight', 'TargetDate', 'CurrentWeight', 'HeightCm', 'AgeYears']
    missing = [r for r in required if r not in data]
    if missing:
        context = f"Setup incomplete: missing {', '.join(missing)}."
        add_message("Monika", ask_monika(f"Whoa, babe! We’re still missing {', '.join(missing)}. Let’s get those next!", context))
        return
    try:
        cursor.execute(
            "EXEC CompleteAppSetup @StartWeight=?, @TargetWeight=?, @TargetDate=?, @CurrentWeight=?, @HeightCm=?, @AgeYears=?",
            (data['StartWeight'], data['TargetWeight'], data['TargetDate'], 
             data['CurrentWeight'], data['HeightCm'], data['AgeYears'])
        )
        cursor.execute("UPDATE Goals SET StartWeight = ?, TargetWeight = ?, TargetDate = ? WHERE GoalType = 'LongTerm' AND Active = 1",
                       (data['StartWeight'], data['TargetWeight'], data['TargetDate']))
        cursor.execute("INSERT INTO WeightLog ([Date], RecordedWeight) VALUES (?, ?)", (datetime.date.today(), data['CurrentWeight']))
        session_state["conn"].commit()
        context = "Day 1 setup completed, moving to food preferences."
        add_message("Monika", ask_monika("Setup’s done, sweetie! Let’s pick your meats next.", context))
        cursor.execute("SELECT COUNT(*) FROM FoodItems WHERE Active = 1")
        has_prefs = cursor.fetchone()[0] > 0
        if not has_prefs:
            cursor.execute("SELECT FoodID, FoodName, Active FROM FoodItems WHERE TotalCarbohydrates = 0 OR TotalCarbohydrates IS NULL")
            food_items = cursor.fetchall()
            if not food_items:
                cursor.execute("INSERT INTO FoodItems (FoodName, TotalCalories, Protein, TotalCarbohydrates, TotalFat, Active) VALUES (?, ?, ?, ?, ?, ?)",
                               ("Beef Ribeye", 75, 8, 0, 5, 1))
                cursor.execute("INSERT INTO FoodItems (FoodName, TotalCalories, Protein, TotalCarbohydrates, TotalFat, Active) VALUES (?, ?, ?, ?, ?, ?)",
                               ("Bacon", 541, 37, 0, 42, 1))
                cursor.execute("INSERT INTO FoodItems (FoodName, TotalCalories, Protein, TotalCarbohydrates, TotalFat, Active) VALUES (?, ?, ?, ?, ?, ?)",
                               ("Ground Beef (80/20)", 307, 17, 0, 26, 1))
                session_state["conn"].commit()
            else:
                prefs = ",".join(f"{food[0]}:{1 if food[2] else 0}" for food in food_items)
                cursor.execute("EXEC UpdateFoodPreferences @FoodPreferences=?", (prefs,))
                session_state["conn"].commit()
            add_message("Monika", ask_monika("Got your carnivore picks, love! Time to plan your day!", "Food prefs set to meat only."))
        session_state["setup_step"] = 100
        generate_today_plan()
    except pyodbc.Error as e:
        add_message("Monika", f"Monika: Setup snag! Error: {e}")
        print(f"SETUP ERROR: {e}")

def start_daily_check():
    cursor = session_state["cursor"]
    cursor.execute("EXEC DailyStartupCheck")
    results = cursor.fetchall()
    if results:
        session_state["daily_prompts"] = [(r[0], r[1], r[2]) for r in results]
        session_state["setup_step"] = 10
        prompt = session_state["daily_prompts"][0][1]
        context = f"Daily startup, step 10, prompt 1 of {len(results)}: asking for {session_state['daily_prompts'][0][2]}."
        add_message("Monika", ask_monika(prompt, context))
        session_state["waiting_for_input"] = True
    else:
        session_state["setup_step"] = 100
        exercise_count, meal_count = get_daily_tally()
        add_message("Monika", ask_monika(f"Hey, babe! You’ve logged {exercise_count} exercises and {meal_count} out of 5 meals today. Any updates?", "Fitness mode daily check"))
        session_state["waiting_for_input"] = True
    session_state["conn"].commit()

def handle_setup(message):
    if not session_state["setup_prompts"] or session_state["setup_step"] > len(session_state["setup_prompts"]):
        complete_setup()
        return
    prompt = session_state["setup_prompts"][session_state["setup_step"] - 1]
    param_name = prompt[2]
    context = f"Day 1 setup step {session_state['setup_step']} of {len(session_state['setup_prompts'])}: expecting {param_name}."
    try:
        if param_name in ['StartWeight', 'TargetWeight', 'CurrentWeight', 'HeightCm', 'AgeYears']:
            value = float(message.split()[0])
            if param_name == 'StartWeight':
                session_state["setup_data"]['StartWeight'] = value
                session_state["setup_data"]['CurrentWeight'] = value
            else:
                session_state["setup_data"][param_name] = value
        elif param_name == 'TargetDate':
            value = message.strip()
            session_state["setup_data"][param_name] = value
        session_state["setup_step"] += 1
        if session_state["setup_step"] <= len(session_state["setup_prompts"]):
            next_prompt = session_state["setup_prompts"][session_state["setup_step"] - 1][1]
            add_message("Monika", ask_monika(next_prompt, context))
            session_state["waiting_for_input"] = True
        else:
            complete_setup()
    except ValueError:
        add_message("Monika", ask_monika(f"Oops, {param_name} needs a {'number' if param_name != 'TargetDate' else 'date'}—try again, love!", context))

def calculate_baseline():
    cursor = session_state["cursor"]
    data = session_state["setup_data"]
    cursor.execute("DECLARE @Calories INT; EXEC CalculateBaselineCalories @CurrentWeight = ?, @HeightCm = ?, @AgeYears = ?, @ActivityLevel = ?, @BaselineCalories = @Calories OUTPUT; SELECT @Calories",
                   (data["CurrentWeight"], data["HeightCm"], data["AgeYears"], data["ActivityLevel"]))
    return cursor.fetchone()[0]

def calculate_deficit():
    cursor = session_state["cursor"]
    data = session_state["setup_data"]
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    cursor.execute("SELECT ISNULL(SUM(CalorieBurn), 0) FROM DailyExerciseTotals WHERE [Date] = ?", (yesterday,))
    burn = cursor.fetchone()[0]
    cursor.execute("SELECT ISNULL(SUM(TotalCalories), 0) FROM ActualMeals WHERE [Date] = ?", (yesterday,))
    intake = cursor.fetchone()[0]
    cursor.execute("DECLARE @Deficit INT; EXEC CalculateDailyDeficit @CurrentWeight = ?, @YesterdayBurn = ?, @YesterdayIntake = ?, @DailyDeficit = @Deficit OUTPUT; SELECT @Deficit",
                   (data["CurrentWeight"], burn, intake))
    return cursor.fetchone()[0]

def handle_daily(message):
    cursor = session_state["cursor"]
    step = session_state["setup_step"]
    if session_state["mode"] == "chat":
        context = get_db_context()
        add_message("Monika", ask_monika(message, context))
        return

    if step == 10 and session_state["daily_prompts"]:
        prompt = session_state["daily_prompts"][0]
        param_name = prompt[2]
        context = f"Daily startup step 10, prompt {session_state['daily_prompts'].index(prompt) + 1} of {len(session_state['daily_prompts'])}: expecting {param_name}."
        try:
            if param_name == 'CurrentWeight':
                weight = float(message.split()[0])
                cursor.execute("INSERT INTO WeightLog ([Date], RecordedWeight) VALUES (?, ?)", (datetime.date.today(), weight))
                session_state["setup_data"]["CurrentWeight"] = weight
            elif param_name == 'YesterdayExercise':
                if "no" in message.lower():
                    pass
                else:
                    match = re.search(r"(\d+(\.\d+)?)\s*(laps|miles|min|minutes)", message.lower())
                    if match:
                        duration = float(match.group(1))
                        exercise_type = "Laps" if "laps" in message.lower() else "Walking"
                        cal_per_lap = 923 / 12
                        calories = int(duration * cal_per_lap) if exercise_type == "Laps" else None
                        cursor.execute(f"EXEC SubmitExercise @ExerciseType=?, @DurationMinutes=?, @CalorieBurn=?", 
                                       (exercise_type, duration if exercise_type != "Laps" else duration * 8 / 12, calories))
                        result = cursor.fetchone()
                        cursor.execute("INSERT INTO ProgressLog (LogDate, Laps, CaloriesBurned, Weight) VALUES (?, ?, ?, ?)",
                                       (result[0], duration if exercise_type == "Laps" else 0, result[1], session_state["setup_data"]["CurrentWeight"]))
                    else:
                        add_message("Monika", ask_monika("Say 'no' or like '12 laps', babe!", context))
                        return
            elif param_name in ['Breakfast', 'Snack1', 'Lunch', 'Snack2', 'Dinner']:
                yesterday = datetime.date.today() - datetime.timedelta(days=1)
                if "nothing" in message.lower() or "skip" in message.lower():
                    pass
                else:
                    match = re.match(r"(\d+)g\s+(.+)", message, re.IGNORECASE)
                    if match:
                        qty, food = match.groups()
                        cursor.execute("SELECT FoodID, TotalCalories FROM FoodItems WHERE FoodName = ? AND Active = 1", (food,))
                        food_data = cursor.fetchone()
                        if food_data:
                            total_cal = (float(qty) / 100) * food_data[1] if food_data[1] else 0
                            meal_name = param_name if param_name in ['Breakfast', 'Lunch', 'Dinner'] else 'Snack'
                            cursor.execute("EXEC LogActual @Date=?, @FoodID=?, @Description=?, @Quantity=?, @TotalCalories=?, @MealName=?",
                                           (yesterday, food_data[0], food, qty, total_cal, meal_name))
                        else:
                            cursor.execute("EXEC LogDeviation @Date=?, @Reason=?", (yesterday, f"Ate non-carnivore {food} for {param_name}"))
                            cursor.execute("EXEC LogActual @Date=?, @FoodID=?, @Description=?, @Quantity=?, @TotalCalories=?, @MealName=?",
                                           (yesterday, None, food, qty, 0, param_name if param_name in ['Breakfast', 'Lunch', 'Dinner'] else 'Snack'))
                    else:
                        add_message("Monika", ask_monika(f"Try '100g Ribeye' or 'nothing' for {param_name}, love!", context))
                        return
            session_state["conn"].commit()
            session_state["daily_prompts"].pop(0)
            if session_state["daily_prompts"]:
                next_prompt = session_state["daily_prompts"][0][1]
                add_message("Monika", ask_monika(next_prompt, context))
                session_state["waiting_for_input"] = True
            else:
                session_state["setup_step"] = 100
                exercise_count, meal_count = get_daily_tally()
                add_message("Monika", ask_monika(f"Got it all, babe! You’ve logged {exercise_count} exercises and {meal_count} out of 5 meals today. Any updates?", context))
                session_state["waiting_for_input"] = True
        except ValueError:
            add_message("Monika", ask_monika(f"Oops, {param_name} needs a number—try again, sweetie!", context))
    elif step == 100:  # Post-plan or ongoing updates
        context = "Step 100: Fitness mode—handling updates or queries."
        now = datetime.datetime.now().hour
        if "what do i eat for" in message.lower():
            meal_type = re.search(r"for\s+(\w+)", message.lower())
            if meal_type and session_state["daily_plan"]:
                meal = meal_type.group(1).capitalize()
                plan = session_state["daily_plan"]["meals"]
                meal_plan = next((m for m in plan if m[1].lower() == meal.lower() or (m[1].lower() == "snack" and meal.lower() == "snack")), None)
                if meal_plan:
                    add_message("Monika", ask_monika(f"For {meal}, dig into {meal_plan[2]} ({meal_plan[3]}g, {meal_plan[4]} cal), babe!", context))
                else:
                    add_message("Monika", ask_monika(f"No {meal} in the plan, love—stick to what I gave you!", context))
        elif "i ate" in message.lower():
            meal_match = re.search(r"i ate\s+(\w+)", message.lower())
            if meal_match:
                meal_type = meal_match.group(1)
                if "plan" in message.lower() or "suggested" in message.lower():
                    log_meal(meal_type, is_plan=True, context=context)
                else:
                    add_message("Monika", ask_monika(f"You ate {meal_type}? What’d you have—give me the meaty details (e.g., '100g Ribeye')!", context))
                    session_state["state"]["pending_meal"] = meal_type
                    session_state["setup_step"] = 13
            else:
                add_message("Monika", ask_monika("You ate what, babe? Say 'I ate breakfast' or something!", context))
        elif re.search(r"(\d+)\s*(laps|miles|min|minutes)", message.lower()):
            match = re.search(r"(\d+(\.\d+)?)\s*(laps|miles|min|minutes)", message.lower())
            duration = float(match.group(1))
            exercise_type = "Laps" if "laps" in message.lower() else "Walking"
            cal_per_lap = 923 / 12
            calories = int(duration * cal_per_lap) if exercise_type == "Laps" else None
            cursor.execute(f"EXEC SubmitExercise @ExerciseType=?, @DurationMinutes=?, @CalorieBurn=?", 
                           (exercise_type, duration if exercise_type != "Laps" else duration * 8 / 12, calories))
            result = cursor.fetchone()
            cursor.execute("INSERT INTO ProgressLog (LogDate, Laps, CaloriesBurned, Weight) VALUES (?, ?, ?, ?)",
                           (result[0], duration if exercise_type == "Laps" else 0, result[1], session_state["setup_data"]["CurrentWeight"]))
            session_state["conn"].commit()
            exercise_count, meal_count = get_daily_tally()
            add_message("Monika", ask_monika(f"Logged {duration} {exercise_type.lower()}, babe! Now at {exercise_count} exercises and {meal_count}/5 meals today. What else?", context))
        elif "fucked up" in message.lower() or "bacanator" in message.lower():
            cursor.execute("EXEC LogDeviation @Date=?, @Reason=?", (datetime.date.today(), "Ate non-carnivore Bacanator combo"))
            cursor.execute("EXEC LogActual @Date=?, @FoodID=?, @Description=?, @Quantity=?, @TotalCalories=?, @MealName=?",
                           (datetime.date.today(), None, "Bacanator combo", 1, 960, "Lunch"))
            session_state["conn"].commit()
            session_state["meals_logged_today"].append("lunch")
            exercise_count, meal_count = get_daily_tally()
            add_message("Monika", ask_monika(f"Oh, you naughty boy—a Bacanator? Logged it, now at {exercise_count} exercises and {meal_count}/5 meals. Back to meat tomorrow?", context))
        elif "didn’t eat" in message.lower() or "skip" in message.lower():
            meal_match = re.search(r"(didn’t eat|skip)\s+(\w+)", message.lower())
            if meal_match:
                meal_type = meal_match.group(2)
                if meal_type.lower() in ["breakfast", "snack", "lunch", "dinner"]:
                    session_state["meals_logged_today"].append(meal_type.lower())
                    exercise_count, meal_count = get_daily_tally()
                    add_message("Monika", ask_monika(f"Skipped {meal_type}? Got it, now at {exercise_count} exercises and {meal_count}/5 meals. What’s next?", context))
                else:
                    add_message("Monika", ask_monika("Skip what, love? Say 'I didn’t eat lunch'!", context))
        elif missing := check_missing_meals() and now >= 7:
            next_meal = missing[0]
            add_message("Monika", ask_monika(f"Hey, babe, it’s {now}:00—you haven’t logged {next_meal} yet. Did you eat it or skip it?", context))
        else:
            exercise_count, meal_count = get_daily_tally()
            add_message("Monika", ask_monika(f"Got your plan, love—you’re at {exercise_count} exercises and {meal_count}/5 meals today. What do you want to update?", context))
    elif step == 13:  # Log meal details
        context = "Step 13: Logging today’s meal details—expecting 'quantity food' or 'no'."
        pending_meal = session_state["state"].get("pending_meal")
        if pending_meal:
            if "no" in message.lower() or "didn’t" in message.lower():
                session_state["meals_logged_today"].append(pending_meal.lower())
                exercise_count, meal_count = get_daily_tally()
                add_message("Monika", ask_monika(f"Skipped {pending_meal}? No worries—now at {exercise_count} exercises and {meal_count}/5 meals today. What else?", context))
                del session_state["state"]["pending_meal"]
                session_state["setup_step"] = 100
            elif re.match(r"(\d+)g\s+(.+)", message, re.IGNORECASE):
                match = re.match(r"(\d+)g\s+(.+)", message, re.IGNORECASE)
                qty, food = match.groups()
                log_meal(pending_meal, food, qty, is_plan=False, context=context)
                del session_state["state"]["pending_meal"]
                session_state["setup_step"] = 100
            else:
                add_message("Monika", ask_monika(f"What’d you eat for {pending_meal}? Say '100g Ribeye' or 'no' if you skipped!", context))
        else:
            add_message("Monika", ask_monika("What meal are we logging, love? Say 'I ate lunch' first!", context))
            session_state["setup_step"] = 100

def log_meal(meal_type, food=None, qty=None, is_plan=True, context=""):
    cursor = session_state["cursor"]
    today = datetime.date.today()
    if meal_type.lower() not in ["breakfast", "snack", "lunch", "dinner"]:
        add_message("Monika", ask_monika(f"{meal_type}? Pick breakfast, snack, lunch, or dinner, babe!", context))
        return
    if meal_type.lower() in session_state["meals_logged_today"]:
        add_message("Monika", ask_monika(f"You already logged {meal_type} today, love—something else?", context))
        return
    
    if is_plan and session_state["daily_plan"]:
        plan = session_state["daily_plan"]["meals"]
        meal_plan = next((m for m in plan if m[1].lower() == meal_type.lower() or (m[1].lower() == "snack" and meal_type.lower() == "snack")), None)
        if meal_plan:
            food, qty, total_cal = meal_plan[2], meal_plan[3], meal_plan[4]
            cursor.execute("SELECT FoodID FROM FoodItems WHERE FoodName = ? AND Active = 1", (food,))
            food_id = cursor.fetchone()[0]
            cursor.execute("EXEC LogActual @Date=?, @FoodID=?, @Description=?, @Quantity=?, @TotalCalories=?, @MealName=?",
                           (today, food_id, food, qty, total_cal, meal_type))
            session_state["conn"].commit()
            session_state["meals_logged_today"].append(meal_type.lower())
            exercise_count, meal_count = get_daily_tally()
            add_message("Monika", ask_monika(f"Logged your {meal_type} as planned: {qty}g {food}, {total_cal} cal. Now at {exercise_count} exercises and {meal_count}/5 meals. What’s next?", context))
    elif food and qty:
        cursor.execute("SELECT FoodID, TotalCalories FROM FoodItems WHERE FoodName = ? AND Active = 1", (food,))
        food_data = cursor.fetchone()
        if food_data:
            total_cal = (float(qty) / 100) * food_data[1] if food_data[1] else 0
            cursor.execute("EXEC LogActual @Date=?, @FoodID=?, @Description=?, @Quantity=?, @TotalCalories=?, @MealName=?",
                           (today, food_data[0], food, qty, total_cal, meal_type))
            session_state["conn"].commit()
            session_state["meals_logged_today"].append(meal_type.lower())
            exercise_count, meal_count = get_daily_tally()
            add_message("Monika", ask_monika(f"Logged {qty}g {food} for {meal_type}, {total_cal} cal. Now at {exercise_count} exercises and {meal_count}/5 meals. Sticking to the plan?", context))
        else:
            cursor.execute("EXEC LogDeviation @Date=?, @Reason=?", (today, f"Ate non-carnivore {food} for {meal_type}"))
            cursor.execute("EXEC LogActual @Date=?, @FoodID=?, @Description=?, @Quantity=?, @TotalCalories=?, @MealName=?",
                           (today, None, food, qty, 0, meal_type))
            session_state["conn"].commit()
            session_state["meals_logged_today"].append(meal_type.lower())
            exercise_count, meal_count = get_daily_tally()
            add_message("Monika", ask_monika(f"{food} for {meal_type}? That’s off the meat menu—logged as a slip-up! Now at {exercise_count} exercises and {meal_count}/5 meals. What else?", context))

def check_missing_meals():
    now = datetime.datetime.now().hour
    expected = []
    if now >= 7 and "breakfast" not in session_state["meals_logged_today"]:
        expected.append("breakfast")
    if now >= 10 and "snack" not in [m for m in session_state["meals_logged_today"] if m == "snack"]:
        expected.append("snack 1")
    if now >= 12 and "lunch" not in session_state["meals_logged_today"]:
        expected.append("lunch")
    if now >= 15 and len([m for m in session_state["meals_logged_today"] if m == "snack"]) < 2:
        expected.append("snack 2")
    if now >= 18 and "dinner" not in session_state["meals_logged_today"]:
        expected.append("dinner")
    return expected

def generate_today_plan():
    cursor = session_state["cursor"]
    baseline = calculate_baseline()
    deficit = calculate_deficit()
    target_calories = baseline - deficit
    context = f"Step 100: Generating today’s carnivore diet plan (5 meals), baseline {baseline} cal, target {target_calories} cal after {deficit} cal deficit based on goals."
    
    cursor.execute("EXEC GenerateDailyPlan")
    results = cursor.fetchall()
    if results and results[0][0]:
        allowance = results[0][0]
        fasting_day = results[0][5]
        if fasting_day:
            plan_text = results[0][2]
            session_state["daily_plan"] = {"text": plan_text, "meals": [(0, "Fasting", "No food", 0, 0)]}
            exercise_count, meal_count = get_daily_tally()
            add_message("Monika", ask_monika(f"{plan_text} Now at {exercise_count} exercises and {meal_count}/5 meals today. Any updates?", context))
        else:
            plan_text = f"Here’s your carnivore feast for today ({allowance:.0f} cal total, aiming for {target_calories} cal):\n" + \
                        "\n".join([f"{row[1]}: {row[2]} ({row[3]}g, {row[4]} cal)" for row in results])
            session_state["daily_plan"] = {"text": plan_text, "meals": [(i, row[1], row[2], row[3], row[4]) for i, row in enumerate(results)]}
            exercise_count, meal_count = get_daily_tally()
            add_message("Monika", ask_monika(f"{plan_text} Now at {exercise_count} exercises and {meal_count}/5 meals today. Any updates?", context))
        session_state["setup_step"] = 100
        session_state["waiting_for_input"] = True
        session_state["meals_logged_today"] = []
    session_state["conn"].commit()

@app.route('/')
def index():
    if session_state["setup_step"] == 0:
        start_setup()
    return render_template('index.html', messages=session_state["messages"], state=session_state["state"])

@app.route('/send', methods=['POST'])
def send_chat():
    user_input = request.form.get('message')
    if not user_input:
        return jsonify({"messages": session_state["messages"], "state": session_state["state"]})
    add_message("Joseph", user_input)
    session_state["waiting_for_input"] = False
    if session_state["mode"] == "fitness" and session_state["setup_step"] > 0 and session_state["setup_step"] < 10:
        handle_setup(user_input)
    elif session_state["mode"] == "fitness" and session_state["setup_step"] >= 10:
        handle_daily(user_input)
    else:
        context = get_db_context()
        add_message("Monika", ask_monika(user_input, context))
    return jsonify({"messages": session_state["messages"], "state": session_state["state"]})

@app.route('/toggle_mode', methods=['POST'])
def toggle_mode():
    new_mode = request.form.get('mode')
    session_state["mode"] = new_mode
    if new_mode == "fitness":
        session_state["setup_step"] = 10 if session_state["setup_step"] < 10 else session_state["setup_step"]
        start_daily_check()
    else:
        context = "Switching to Chat mode."
        add_message("Monika", ask_monika("Hey, sweetie! Just you and me now—what’s on your mind?", context))
    return jsonify({"messages": session_state["messages"]})

if __name__ == "__main__":
    app.run(debug=True)