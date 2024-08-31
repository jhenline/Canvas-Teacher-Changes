# Author: Jeff Henline (1/12/24)
# Last change: 1/13/24 - Remove console print statements, added printing of date
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

# API Configuration
API_URL = 'https://calstatela.instructure.com/api/v1'
ENROLLMENT_TERM_ID = '335'  # Fall 2024
ACCOUNT_ID = '1'


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
    instructors = fetch_instructors_for_course(course_id, headers)
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

    # Parallel fetch for courses
    courses_endpoint = f"{API_URL}/accounts/{ACCOUNT_ID}/courses"
    courses_params = {'enrollment_term_id': ENROLLMENT_TERM_ID, 'per_page': 100}
    with concurrent.futures.ThreadPoolExecutor() as executor:
        courses_future = executor.submit(fetch_courses, headers, courses_endpoint, courses_params)
        courses = courses_future.result()

    # Parallel fetch for instructors for each course
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_instructors = {executor.submit(fetch_instructors_for_course, headers, course['id']): course for course in courses}
        for future in concurrent.futures.as_completed(future_to_instructors):
            course = future_to_instructors[future]
            teacher_names = future.result()
            teachers[course['name']] = teacher_names

    return teachers

def fetch_instructors_for_course(headers, course_id):
    """
    Fetch all instructors for a given course with pagination.
    """
    instructors_endpoint = f"{API_URL}/courses/{course_id}/users"
    instructors_params = {'enrollment_type': ['teacher'], 'per_page': 100}
    instructors = []
    while instructors_endpoint:
        response = requests.get(instructors_endpoint, headers=headers, params=instructors_params)
        response.raise_for_status()
        instructors.extend(response.json())
        instructors_endpoint = get_next_link(response.headers.get('Link'))
    return {instructor['name'] for instructor in instructors}

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


def compare_teachers(old_list, new_list, db_connection):
    for course, new_teachers in new_list.items():
        old_teachers = old_list.get(course, set())

        # Removed teachers
        removed_teachers = old_teachers - new_teachers
        for teacher in removed_teachers:
            print(f"In {course}, {teacher} was removed")
            log_teacher_change(db_connection, course, 'removed', teacher)

        # Added teachers
        added_teachers = new_teachers - old_teachers
        for teacher in added_teachers:
            print(f"In {course}, {teacher} was added")
            log_teacher_change(db_connection, course, 'added', teacher)


def main():
    print(f"Started at {datetime.datetime.now()}")  # Print the start time

    db_connection = create_db_connection()  # Establish database connection
    if db_connection is None:
        print("Failed to connect to the database. Exiting.")
        return

    current_teachers = fetch_current_teachers()
    previous_teachers = load_teachers_list()

    if previous_teachers is not None:
        compare_teachers(previous_teachers, current_teachers, db_connection)  # Pass db_connection here
    else:
        print("No previous data found. Saving current list.")

    save_teachers_list(current_teachers)

    db_connection.close()  # Close the database connection

    print(f"Finished at {datetime.datetime.now()}")  # Print the end time

if __name__ == "__main__":
    main()
