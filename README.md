# Canvas Teacher Changes Tracker

This script tracks changes in the list of instructors associated with courses for a specified term in Canvas. The first time the script runs, it generates a list of courses and their associated teachers. On subsequent runs, it compares the current list with the previous list, logs any changes, and writes the results to a MySQL database.

## Features

- **Initial Data Generation**: The script generates an initial list of courses and their associated teachers.
- **Change Detection**: On subsequent runs, the script compares the current list of teachers with the previous one, identifying any additions or removals.
- **Parallel Execution**: The script uses Python's `concurrent.futures` for parallelized fetching of courses and instructors, significantly reducing execution time.
- **Database Logging**: Changes in the list of instructors are logged into a MySQL database.

## Requirements

- Python 3.x
- Required Python packages:
  - `requests`
  - `mysql-connector-python`
  - `configparser`
