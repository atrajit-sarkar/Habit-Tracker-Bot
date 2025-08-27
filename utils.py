import datetime
import calendar
import os
from string import Template


def format_progress_message(db, user_id: int, today: datetime.date) -> str:
    year = today.year
    month = today.month

    dates = db.get_dates_for_month(user_id, year, month)
    days = set()
    for d in dates:
        try:
            parts = d.split('-')
            days.add(int(parts[2]))
        except Exception:
            continue

    cal = calendar.monthcalendar(year, month)
    # Build a simple visual: weeks with ✔ for studied, · for not
    lines = []
    lines.append(f"<b>Habit Progress — {today.strftime('%B %Y')}</b>")
    lines.append(f"Total days recorded this month: <b>{len(days)}</b>")
    lines.append("")
    lines.append("Mo Tu We Th Fr Sa Su")

    for week in cal:
        week_str = []
        for d in week:
            if d == 0:
                week_str.append('  ')
            else:
                if d in days:
                    # use emoji checkmark
                    week_str.append('✅')
                else:
                    week_str.append('·')
        lines.append(' '.join(week_str))

    total = db.total_days(user_id)
    lines.append("")
    lines.append(f"Lifetime streak days recorded: <b>{total}</b>")

    # Small motivational blurb
    pct = (len(days) / calendar.monthrange(year, month)[1]) * 100
    lines.append("")
    lines.append(f"You're at <b>{pct:.0f}%</b> of this month — keep going!")

    return '\n'.join(lines)


def generate_dashboard_html(db, user_id: int, today: datetime.date) -> str:
    """Generate a comprehensive dashboard with all tasks and analytics"""
    # Get all user tasks
    tasks = db.get_user_tasks(user_id)
    
    # Calculate overall stats
    total_tasks = len(tasks)
    total_completions = db.total_days(user_id)
    this_month_completions = len(db.get_dates_for_month(user_id, today.year, today.month))
    
    # Calculate completion rate (this month)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    completion_rate = int((this_month_completions / (days_in_month * total_tasks)) * 100) if total_tasks > 0 else 0
    
    # Generate motivational message
    if completion_rate >= 80:
        motivational_message = "You're absolutely crushing it! Keep up the amazing work!"
    elif completion_rate >= 60:
        motivational_message = "Great progress! You're building strong habits!"
    elif completion_rate >= 40:
        motivational_message = "Good start! Every step counts towards your goals!"
    else:
        motivational_message = "Every journey begins with a single step. You've got this!"
    
    # Generate task cards
    task_cards_html = []
    for task in tasks:
        task_id, name, description, frequency, schedule_time, created_at = task
        task_completions = db.total_days(user_id, task_id)
        month_completions = len(db.get_dates_for_month(user_id, today.year, today.month, task_id))
        
        # Generate mini calendar
        month_dates = db.get_dates_for_month(user_id, today.year, today.month, task_id)
        completed_days = set()
        for date_str in month_dates:
            try:
                day = int(date_str.split('-')[2])
                completed_days.add(day)
            except:
                continue
        
        cal = calendar.monthcalendar(today.year, today.month)
        calendar_html = ""
        for week in cal:
            for day in week:
                if day == 0:
                    calendar_html += '<div class="calendar-day empty"></div>'
                elif day in completed_days:
                    calendar_html += f'<div class="calendar-day completed">{day}</div>'
                else:
                    calendar_html += f'<div class="calendar-day pending">{day}</div>'
        
        # Calculate progress percentage
        expected_completions = days_in_month if frequency == 'daily' else days_in_month // 7 if frequency == 'weekly' else 1
        progress_pct = min(100, int((month_completions / expected_completions) * 100)) if expected_completions > 0 else 0
        
        schedule_display = f"⏰ {schedule_time}" if schedule_time else "📅 Manual"
        
        task_card = f"""
        <div class="task-card {'completed' if progress_pct >= 80 else ''}" data-task-id="{task_id}">
            <div class="task-name">{name}</div>
            <div class="task-description">{description}</div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: {progress_pct}%"></div>
            </div>
            <div class="task-meta">
                <span class="task-frequency">{frequency.title()}</span>
                <span>{schedule_display}</span>
            </div>
            <div class="task-meta">
                <span>📊 {task_completions} total</span>
                <span>📅 {month_completions}/{expected_completions} this month</span>
            </div>
            <div class="calendar-mini">
                {calendar_html}
            </div>
        </div>
        """
        task_cards_html.append(task_card)
    
    # Generate comparison bars
    comparison_bars_html = []
    max_completions = max((db.total_days(user_id, task[0]) for task in tasks), default=1)
    
    for task in tasks:
        task_id, name, description, frequency, schedule_time, created_at = task
        task_completions = db.total_days(user_id, task_id)
        percentage = int((task_completions / max_completions) * 100) if max_completions > 0 else 0
        
        comparison_bar = f"""
        <div class="task-progress-row">
            <div class="task-progress-name">{name}</div>
            <div class="task-progress-bar">
                <div class="task-progress-fill" style="width: {percentage}%"></div>
                <div class="task-progress-text">{task_completions} days</div>
            </div>
        </div>
        """
        comparison_bars_html.append(comparison_bar)
    
    # Load template file
    template_path = os.path.join(os.path.dirname(__file__), 'dashboard_template.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        template_content = f.read()
    
    # Use string.Template for safe substitution
    template = Template(template_content)
    
    # Substitute variables
    html = template.substitute(
        total_tasks=total_tasks,
        total_completions=total_completions,
        this_month_completions=this_month_completions,
        completion_rate=completion_rate,
        motivational_message=motivational_message,
        task_cards='\n'.join(task_cards_html),
        comparison_bars='\n'.join(comparison_bars_html)
    )
    
    return html


def generate_progress_html(db, user_id: int, today: datetime.date, task_id: int = None) -> str:
    """Return a full HTML page (string) with a visually appealing progress card for the month."""
    year = today.year
    month = today.month

    dates = db.get_dates_for_month(user_id, year, month, task_id)
    days = set()
    for d in dates:
        try:
            parts = d.split('-')
            days.add(int(parts[2]))
        except Exception:
            continue

    month_name = today.strftime('%B %Y')
    month_len = calendar.monthrange(year, month)[1]
    done_count = len(days)
    percentage = int((done_count / month_len) * 100) if month_len else 0
    lifetime_days = db.total_days(user_id, task_id)

    # Get task name if specific task
    task_name = "All Tasks"
    if task_id:
        task_info = db.get_task_by_id(task_id, user_id)
        if task_info:
            task_name = task_info[1]

    # Build calendar table rows
    cal = calendar.monthcalendar(year, month)
    cal_rows = []
    
    for week in cal:
        cells = []
        for d in week:
            if d == 0:
                cells.append('<td class="empty">&nbsp;</td>')
            else:
                cls = 'done' if d in days else 'day'
                cells.append(f'<td class="{cls}">{d}</td>')
        cal_rows.append('<tr>' + ''.join(cells) + '</tr>')
    
    calendar_rows_html = '\n                        '.join(cal_rows)

    # Load template file
    template_path = os.path.join(os.path.dirname(__file__), 'progress_template.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        template_content = f.read()
    
    # Use string.Template for safe substitution
    template = Template(template_content)
    
    # Titles
    display_title = month_name if not task_id else f"{task_name} - {month_name}"
    page_title = "Progress" if not task_id else f"{task_name} Progress"
    calendar_title = "Calendar" if not task_id else f"{task_name} Calendar"
    
    # Substitute variables
    html = template.substitute(
        title=page_title,
        month_name=display_title,
        done_count=done_count,
        total_days=month_len,
        lifetime_days=lifetime_days,
        percentage=percentage,
        calendar_rows=calendar_rows_html,
        calendar_title=calendar_title
    )

    return html