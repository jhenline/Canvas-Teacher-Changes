# Canvas Teacher Changes Tracker

This script tracks changes in the list of instructors associated with Canvas courses for a specified academic term. It is designed to be run repeatedly (for example, via cron) to detect when instructors are **added to or removed from courses** and log those changes to a MySQL database.

On the **first run for a term**, the script establishes a baseline snapshot of courses and instructors. On **subsequent runs**, it compares the current state against that baseline and records any changes.

---

## Features

- **Initial Baseline Generation**
  - On first run, the script generates and saves a list of courses and their associated instructors.
  - No database changes are logged on the first run.

- **Change Detection**
  - On subsequent runs, the script detects:
    - Instructors added to a course
    - Instructors removed from a course

- **Parallel Execution**
  - Uses Python’s `concurrent.futures` to fetch courses and instructors in parallel for improved performance.

- **Database Logging**
  - Instructor additions and removals are logged to a MySQL table (`teacher_changes`).

---

## Requirements

- Python 3.x
- Required Python packages:
  - `requests`
  - `mysql-connector-python`
  - `configparser`

---

## Configuration

The script reads configuration values from a `config.ini` file. This file must contain:

- MySQL connection details
- Canvas API token

Example structure:

```ini
[mysql]
DB_HOST=localhost
DB_USER=canvas_user
DB_PASSWORD=securepassword
DB_DATABASE=canvas_db

[auth]
token=YOUR_CANVAS_API_TOKEN
