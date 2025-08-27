# Advanced Habit Tracker Telegram Bot

This repository contains a comprehensive Telegram bot for tracking multiple habits with beautiful analytics, scheduling, and progress visualization.

## Features

### 🎯 Task Management
- Create custom habits to track (exercise, reading, meditation, etc.)
- Delete and manage your tasks
- Individual task progress tracking

### 📊 Beautiful Analytics Dashboard
- **HTML Progress Reports**: Modern, responsive HTML files with your monthly progress
- **Multiple Chart Types**: Pie charts, bar charts, line graphs, and heatmaps
- **Individual & Cumulative Views**: Track specific tasks or overall progress
- **Visual Calendar**: Month view with completed days highlighted

### ⏰ Smart Scheduling
- Set up automated daily polls for each task
- Custom time scheduling (e.g., "09:00" for morning habits)
- Multiple schedules for different tasks

### 📈 Advanced Analytics
- Monthly progress tracking with percentages
- Lifetime statistics for each habit
- Visual heatmaps showing daily completion patterns
- Trend analysis with line charts
- Task distribution with pie charts

## Files Structure

- `bot.py` - Main bot with all commands and handlers
- `db.py` - SQLite database management (tasks, streaks, schedules)
- `utils.py` - HTML progress report generation
- `chart_generator.py` - Chart and visualization generation
- `progress_template.html` - Beautiful HTML template for progress reports
- `requirements.txt` - Python dependencies

## Setup (Windows PowerShell)

1. Install dependencies:
```powershell
python -m pip install -r requirements.txt
```

2. Set your Telegram bot token:
```powershell
$env:TELEGRAM_BOT_TOKEN = 'your_bot_token_here'
```

3. Run the bot:
```powershell
python bot.py
```

## Commands

### 📋 Basic Commands
- `/start` or `/help` - Show all available commands
- `/sendpoll` - Send today's habit poll
- `/progress` - Get current month progress report (HTML)

### 📝 Task Management
- `/tasks` - View all your habits
- `/newtask` - Create a new habit to track
- `/deltask` - Delete a habit

### ⏰ Scheduling
- `/schedule` - Set up automated daily polls
- `/schedules` - View your scheduled polls
- `/delschedule` - Remove a schedule

### 📊 Analytics & Charts
- `/dashboard` - Interactive analytics dashboard
- `/charts` - Generate various progress charts
- `/taskprogress [task_id]` - Individual task progress

## Chart Types Available

1. **🥧 Pie Chart** - Task distribution showing relative completion
2. **📊 Bar Chart** - Comparison between tasks (total vs this month)
3. **📈 Line Chart** - Progress trend over last 30 days
4. **🔥 Heatmap** - Monthly calendar view with daily completion status

## Usage Examples

1. **Create a new habit:**
   - Send `/newtask`
   - Enter habit name: "Morning Exercise"
   - Enter description: "30 minutes of cardio or strength training"

2. **Set up daily reminders:**
   - Send `/schedule`
   - Choose your habit
   - Set time: "07:00"

3. **Track daily progress:**
   - Receive automated polls or use `/sendpoll`
   - Vote "Yes" when you complete the habit
   - Get beautiful HTML progress report automatically

4. **View analytics:**
   - Use `/dashboard` for interactive options
   - Use `/charts` for specific chart types
   - Use `/taskprogress 1` for specific task analysis

## Database Schema

- **tasks**: User habits with descriptions
- **streaks**: Daily completion records per task
- **schedules**: Automated poll schedules

## Customization

- Edit `progress_template.html` for custom HTML styling
- Modify chart colors and styles in `chart_generator.py`
- Add new chart types or analytics in the chart generator

## Advanced Features

- **Multi-task support**: Track unlimited habits simultaneously
- **Responsive design**: HTML reports work on all devices
- **Data persistence**: SQLite database stores all your progress
- **Visual motivation**: Beautiful charts and progress tracking
- **Flexible scheduling**: Set different reminder times for each habit

The bot creates a comprehensive habit tracking system with professional-quality analytics and reporting!
