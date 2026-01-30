# Author: Jeff Henline (1/12/24)
# 1/13/24 - Remove console print statements, added printing of date
# 1/30/26 - Added SendGrid email notification for teacher changes (email address is hardcoded)
#
# The first time this script runs, it generates a list of courses and associated teachers for a given
# term in Canvas. Each subsequent time the script runs, it compares the current associated teachers
# against the previous list and writes the changes to FDMS
# Script takes about 5 mins to run in parallelized

import json
import os
import requests
import mysql.connector
from mysql.connector import Error
import datetime
import concurrent.futures
import configparser

# Configuration for ConfigParser
config = configparser.ConfigParser()

# Read the config.ini file
config.read('/home/bitnami/scripts/config.ini')

# Retrieve MySQL configuration and API key
db_config = config['mysql']
API_KEY = config['auth']['token']
SENDGRID_API_KEY = config['auth'].get('sendgrid_api_key', '').strip()

# API Configuration
API_URL = 'https://calstatela.instructure.com/api/v1'
ENROLLMENT_TERM_ID = '349'  # Spring 2026
ACCOUNT_ID = '10'


def create_db_connection():
    try:
        # Connect to the database using values from the config file
        connection = mysql.connector.connect(
            host=db_config['DB_HOST'],
            user=db_config['DB_USER'],
            password=db_config['DB_PASSWORD'],
            database=db_config['DB_DATABASE']
        )
        return connection
    except Error as e:
        print(f"Error: {e}")
        return None


def fetch_instructors_for_single_course(course, headers):
    """
    Fetch instructors for a single course.
    """
    course_id = course['id']
    instructors, skipped = fetch_instructors_for_course(headers, course_id)
    if skipped:
        print(f"Skipped course {course_id} ({course['name']}) due to 404.", flush=True)
    return course['name'], instructors


def log_teacher_change(connection, course, action, teacher):
    """
    Log teacher changes to the database.
    """
    query = """
    INSERT INTO teacher_changes (course_name, action, teacher)
    VALUES (%s, %s, %s)
    """
    cursor = connection.cursor()
    cursor.execute(query, (course, action, teacher))
    connection.commit()
    cursor.close()


def send_teacher_change_summary_email(changes):
    """
    Send a summary email notification via SendGrid for teacher changes.
    """
    if not SENDGRID_API_KEY:
        print("SendGrid API key not configured. Skipping email.", flush=True)
        return

    if not changes:
        return

    lines = ["Teacher changes detected:\n"]
    for change in changes:
        lines.append(
            f"- Course: {change['course']} | Action: {change['action']} | "
            f"Teacher: {change['teacher']} | Source: {change['source']}"
        )
    lines.append(f"\nTimestamp: {datetime.datetime.now()}")

    email_payload = {
        "personalizations": [
            {
                "to": [{"email": "jhenlin2@calstatela.edu"}],
                "subject": "Canvas teacher change summary"
            }
        ],
        "from": {"email": "no-reply@calstatela.edu"},
        "content": [
            {
                "type": "text/plain",
                "value": "\n".join(lines)
            }
        ]
    }

    response = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json"
        },
        json=email_payload
    )

    if response.status_code >= 300:
        print(
            f"SendGrid email failed ({response.status_code}): {response.text}",
            flush=True
        )


def fetch_courses(headers, courses_endpoint, courses_params):
    """
    Fetch all courses with pagination.
    """
    courses = []
    while courses_endpoint:
        response = requests.get(courses_endpoint, headers=headers, params=courses_params)
        response.raise_for_status()
        courses.extend(response.json())
        courses_endpoint = get_next_link(response.headers.get('Link'))
    return courses


def fetch_current_teachers():
    headers = {'Authorization': f'Bearer {API_KEY}'}
    teachers = {}
    course_ids_by_name = {}

    # Parallel fetch for courses
    courses_endpoint = f"{API_URL}/accounts/{ACCOUNT_ID}/courses"
    courses_params = {'enrollment_term_id': ENROLLMENT_TERM_ID, 'per_page': 100}
    with concurrent.futures.ThreadPoolExecutor() as executor:
        courses_future = executor.submit(fetch_courses, headers, courses_endpoint, courses_params)
        courses = courses_future.result()

    # Parallel fetch for instructors for each course
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_instructors = {
            executor.submit(fetch_instructors_for_course, headers, course['id']): course
            for course in courses
        }
        for future in concurrent.futures.as_completed(future_to_instructors):
            course = future_to_instructors[future]
            teacher_names, skipped = future.result()
            teachers[course['name']] = teacher_names
            course_ids_by_name[course['name']] = course['id']
            if skipped:
                print(f"Skipped course {course['id']} ({course['name']}) due to 404.", flush=True)

    return teachers, course_ids_by_name


def fetch_teacher_sis_import_id(course_id, teacher_name):
    """
    Fetch the SIS import id for a teacher enrollment in a course.
    Returns None if not found or if enrollment is not SIS-created.
    """
    headers = {'Authorization': f'Bearer {API_KEY}'}
    enrollments_endpoint = f"{API_URL}/courses/{course_id}/enrollments"
    enrollments_params = {
        'type[]': ['TeacherEnrollment'],
        'state[]': ['active', 'invited'],
        'include[]': ['user'],
        'per_page': 100
    }

    while enrollments_endpoint:
        response = requests.get(enrollments_endpoint, headers=headers, params=enrollments_params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        enrollments = response.json()

        for enrollment in enrollments:
            user = enrollment.get('user', {})
            if user.get('name') == teacher_name or user.get('sortable_name') == teacher_name:
                return enrollment.get('sis_import_id')

        enrollments_endpoint = get_next_link(response.headers.get('Link'))

    return None

def fetch_instructors_for_course(headers, course_id):
    """
    Fetch all instructors for a given course with pagination.
    """
    instructors_endpoint = f"{API_URL}/courses/{course_id}/users"
    instructors_params = {
        'enrollment_type[]': ['teacher'],
        'enrollment_state[]': ['active', 'invited'],
        'per_page': 100
    }
    instructors = []
    while instructors_endpoint:
        response = requests.get(instructors_endpoint, headers=headers, params=instructors_params)
        if response.status_code == 404:
            return set(), True
        response.raise_for_status()
        instructors.extend(response.json())
        instructors_endpoint = get_next_link(response.headers.get('Link'))
    return {instructor['name'] for instructor in instructors}, False

def get_next_link(link_header):
    """
    Extract the 'next' link from the Link header.
    """
    if link_header:
        links = link_header.split(',')
        for link in links:
            if 'rel="next"' in link:
                next_link = link.split(';')[0].strip('<>')
                return next_link
    return None


def save_teachers_list(teachers, filename='teachers_list.json'):
    """ Save the teachers list to a file in JSON format, converting sets to lists. """
    # Convert sets to lists for JSON serialization
    teachers_for_json = {course: list(teachers) for course, teachers in teachers.items()}

    with open(filename, 'w') as file:
        json.dump(teachers_for_json, file)


def load_teachers_list(filename='teachers_list.json'):
    """ Load the teachers list from a file, converting lists back to sets. """
    if os.path.exists(filename):
        with open(filename, 'r') as file:
            teachers = json.load(file)
            # Convert lists back to sets
            return {course: set(teachers) for course, teachers in teachers.items()}
    return None


def compare_teachers(old_list, new_list, course_ids_by_name, db_connection):
    changes = []
    for course, new_teachers in new_list.items():
        old_teachers = old_list.get(course, set())
        course_id = course_ids_by_name.get(course)

        # Removed teachers
        removed_teachers = old_teachers - new_teachers
        for teacher in removed_teachers:
            print(f"In {course}, {teacher} was removed")
            log_teacher_change(db_connection, course, 'removed', teacher)
            sis_import_id = fetch_teacher_sis_import_id(course_id, teacher) if course_id else None
            source = "SIS import" if sis_import_id else "manual/API (sis_import_id is null)"
            changes.append(
                {
                    "course": course,
                    "action": "removed",
                    "teacher": teacher,
                    "source": source
                }
            )

        # Added teachers
        added_teachers = new_teachers - old_teachers
        for teacher in added_teachers:
            print(f"In {course}, {teacher} was added")
            log_teacher_change(db_connection, course, 'added', teacher)
            sis_import_id = fetch_teacher_sis_import_id(course_id, teacher) if course_id else None
            source = "SIS import" if sis_import_id else "manual/API (sis_import_id is null)"
            changes.append(
                {
                    "course": course,
                    "action": "added",
                    "teacher": teacher,
                    "source": source
                }
            )

    return changes


def main():
    print(f"Started at {datetime.datetime.now()}")  # Print the start time

    db_connection = create_db_connection()  # Establish database connection
    if db_connection is None:
        print("Failed to connect to the database. Exiting.")
        return

    current_teachers, course_ids_by_name = fetch_current_teachers()
    previous_teachers = load_teachers_list()

    if previous_teachers is not None:
        changes = compare_teachers(
            previous_teachers,
            current_teachers,
            course_ids_by_name,
            db_connection
        )
        send_teacher_change_summary_email(changes)
    else:
        print("No previous data found. Saving current list.")

    save_teachers_list(current_teachers)

    db_connection.close()  # Close the database connection

    print(f"Finished at {datetime.datetime.now()}")  # Print the end time

if __name__ == "__main__":
    main()
