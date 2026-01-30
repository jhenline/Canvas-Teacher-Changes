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
TEST_MODE = True
TEST_COURSE_ID = '107746'


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
        course_text = change["course"]
        if change.get("course_url"):
            course_text = f"{course_text} ({change['course_url']})"

        teacher_text = change["teacher"]
        if change.get("teacher_url"):
            teacher_text = f"{teacher_text} ({change['teacher_url']})"

        lines.append(
            f"- Course: {course_text} | Action: {change['action']} | "
            f"Teacher: {teacher_text} | Source: {change['source']}"
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
    if TEST_MODE:
        courses = [{"id": TEST_COURSE_ID, "name": f"Test course {TEST_COURSE_ID}"}]
    else:
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
            teachers_by_id, skipped = future.result()
            teachers[course['name']] = teachers_by_id
            course_ids_by_name[course['name']] = course['id']
            if skipped:
                print(f"Skipped course {course['id']} ({course['name']}) due to 404.", flush=True)

    return teachers, course_ids_by_name


def fetch_teacher_sis_import_id(course_id, teacher_id):
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
            if user.get('id') == teacher_id:
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
            return {}, True
        response.raise_for_status()
        instructors.extend(response.json())
        instructors_endpoint = get_next_link(response.headers.get('Link'))
    return {instructor['id']: instructor['name'] for instructor in instructors}, False

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
    """ Save the teachers list to a file in JSON format. """
    teachers_for_json = {}
    for course, teachers_by_id in teachers.items():
        teachers_for_json[course] = [
            {"id": teacher_id, "name": teacher_name}
            for teacher_id, teacher_name in teachers_by_id.items()
        ]
    with open(filename, 'w') as file:
        json.dump(teachers_for_json, file)


def load_teachers_list(filename='teachers_list.json'):
    """ Load the teachers list from a file (supports legacy and current formats). """
    if os.path.exists(filename):
        with open(filename, 'r') as file:
            teachers = json.load(file)
            normalized = {}
            for course, entries in teachers.items():
                # Legacy format: list of names
                if entries and isinstance(entries[0], str):
                    normalized[course] = set(entries)
                    continue

                # Current format: list of {id, name}
                teachers_by_id = {}
                for entry in entries:
                    teacher_id = entry.get("id")
                    teacher_name = entry.get("name")
                    if teacher_id is not None and teacher_name:
                        teachers_by_id[int(teacher_id)] = teacher_name
                normalized[course] = teachers_by_id
            return normalized
    return None


def compare_teachers(old_list, new_list, course_ids_by_name, db_connection):
    changes = []
    for course, new_teachers in new_list.items():
        old_teachers = old_list.get(course, set())
        course_id = course_ids_by_name.get(course)
        course_url = f"{API_URL.replace('/api/v1', '')}/courses/{course_id}/users" if course_id else None

        # Legacy support: old list stored as names
        if isinstance(old_teachers, set):
            new_names = {name for name in new_teachers.values()}
            removed_names = old_teachers - new_names
            added_names = new_names - old_teachers

            for teacher_name in removed_names:
                print(f"In {course}, {teacher_name} was removed")
                log_teacher_change(db_connection, course, 'removed', teacher_name)
                changes.append(
                    {
                        "course": course,
                        "course_id": course_id,
                        "course_url": course_url,
                        "action": "removed",
                        "teacher": teacher_name,
                        "teacher_id": None,
                        "teacher_url": None,
                        "source": "unknown (legacy data)"
                    }
                )

            for teacher_name in added_names:
                teacher_id = next(
                    (tid for tid, name in new_teachers.items() if name == teacher_name),
                    None
                )
                print(f"In {course}, {teacher_name} was added")
                log_teacher_change(db_connection, course, 'added', teacher_name)
                sis_import_id = (
                    fetch_teacher_sis_import_id(course_id, teacher_id)
                    if course_id and teacher_id
                    else None
                )
                source = "SIS import" if sis_import_id else "manual/API (sis_import_id is null)"
                teacher_url = (
                    f"{API_URL.replace('/api/v1', '')}/users/{teacher_id}"
                    if teacher_id
                    else None
                )
                changes.append(
                    {
                        "course": course,
                        "course_id": course_id,
                        "course_url": course_url,
                        "action": "added",
                        "teacher": teacher_name,
                        "teacher_id": teacher_id,
                        "teacher_url": teacher_url,
                        "source": source
                    }
                )
            continue

        # Removed teachers
        removed_teachers = set(old_teachers.keys()) - set(new_teachers.keys())
        for teacher_id in removed_teachers:
            teacher_name = old_teachers.get(teacher_id, "Unknown")
            print(f"In {course}, {teacher_name} was removed")
            log_teacher_change(db_connection, course, 'removed', teacher_name)
            sis_import_id = fetch_teacher_sis_import_id(course_id, teacher_id) if course_id else None
            source = "SIS import" if sis_import_id else "manual/API (sis_import_id is null)"
            teacher_url = f"{API_URL.replace('/api/v1', '')}/users/{teacher_id}"
            changes.append(
                {
                    "course": course,
                    "course_id": course_id,
                    "course_url": course_url,
                    "action": "removed",
                    "teacher": teacher_name,
                    "teacher_id": teacher_id,
                    "teacher_url": teacher_url,
                    "source": source
                }
            )

        # Added teachers
        added_teachers = set(new_teachers.keys()) - set(old_teachers.keys())
        for teacher_id in added_teachers:
            teacher_name = new_teachers.get(teacher_id, "Unknown")
            print(f"In {course}, {teacher_name} was added")
            log_teacher_change(db_connection, course, 'added', teacher_name)
            sis_import_id = fetch_teacher_sis_import_id(course_id, teacher_id) if course_id else None
            source = "SIS import" if sis_import_id else "manual/API (sis_import_id is null)"
            teacher_url = f"{API_URL.replace('/api/v1', '')}/users/{teacher_id}"
            changes.append(
                {
                    "course": course,
                    "course_id": course_id,
                    "course_url": course_url,
                    "action": "added",
                    "teacher": teacher_name,
                    "teacher_id": teacher_id,
                    "teacher_url": teacher_url,
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
