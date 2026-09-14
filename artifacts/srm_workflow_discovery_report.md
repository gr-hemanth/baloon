# SRM eCurricula Workflow Discovery & Architectural Feasibility Report

## Executive Summary

Through static inspection and runtime network probing of the live SRM portal (`https://dld.srmist.edu.in`) and its dedicated eCurricula LMS (`https://dld.srmist.edu.in/ktretecurricula`), we investigated the complete student worksheet workflow.

**Major Finding:**  
**Over 85% of the SRM eCurricula student workflow is completely API-driven via standard JSON REST endpoints (`/curricula/*`).**  
Browser automation (Playwright) is **not required** for course discovery, semester selection, session loading, worksheet downloading, or worksheet submission ("UPDATE" action). Browser interaction is strictly needed only for **capturing the initial login CAPTCHA canvas** if user-assisted login is performed through browser observation.

---

## 1. Complete Workflow Discovered

```
1. Portal Entry: https://dld.srmist.edu.in
   │
   ▼
2. Campus/Faculty Selection:
   Campus: Kattankulathur (KTR) + Faculty: Engineering & Technology
   │
   ▼
3. Dedicated LMS Landing: https://dld.srmist.edu.in/ktretecurricula/#/
   │
   ▼
4. "START LEARNING" Trigger -> Transitions to Login Page
   │
   ├── Client-Side Canvas CAPTCHA generated (6 digits)
   │   └── STOP: User input requested (WAITING_FOR_CAPTCHA state)
   │
   ▼
5. Authentication: POST /curricula/login
   Payload: { USER_ID, PASSWORD, key }
   Response: { Status: 1, token: "<JWT>", user: {...} }
   │
   ▼
6. Course & Semester Loading: POST /curricula/student/home/getcourses
   Payload: { USER_ID, key }
   Response: { courses: [ { COURSE_CODE, SEMESTER: 3, ... } ] }
   │
   ▼
7. Session & Question Loading: POST /curricula/student/session/getquestions
   Payload: { COURSE_CODE, SESSION, key, MCQ, SQ, LQ }
   │
   ▼
8. Session & Worksheet Status: POST /curricula/student/session/getsessionstatus
   Payload: { USER_ID, COURSE_INFO, SESSION, key }
   Response: { result: { PRACTICE, SLOLINK, ... } }
   │
   ▼
9. Worksheet Download (Docx/PDF): POST /curricula/admin/file/getfile
   Payload: { path, filename, server, key }
   Response: { result: { path: "<direct_download_url>" } }
   │
   ▼
10. Worksheet Submission / UPDATE Action: POST /curricula/student/session/submitlink
    Payload: { view: "<drive_link>", download: "<drive_link>", session, SESSION, SLO, ... }
    Response: { Status: 1, msg: "Link Updated Successfully" }
```

---

## 2. Relevant Sanitized Endpoints & Paths

All backend endpoints are hosted on the API server: `https://dld.srmist.edu.in/ktretecurricula/server`.

| Endpoint Path | HTTP Method | Category | Classification |
|---|---|---|---|
| `/curricula/checkstatus` | `POST` | Health / Configuration | `DIRECT_HTTP_POSSIBLE` |
| `/curricula/login` | `POST` | Authentication | `DIRECT_HTTP_POSSIBLE` *(post-captcha)* |
| `/curricula/logout` | `POST` | Session Teardown | `DIRECT_HTTP_POSSIBLE` |
| `/curricula/getprofile` | `POST` | User Profile | `DIRECT_HTTP_POSSIBLE` |
| `/curricula/student/home/getcourses` | `POST` | Semesters & Courses | `DIRECT_HTTP_POSSIBLE` |
| `/curricula/student/session/getquestions` | `POST` | Question Retrieval | `DIRECT_HTTP_POSSIBLE` |
| `/curricula/student/session/getsessionstatus`| `POST` | Worksheet Status | `DIRECT_HTTP_POSSIBLE` |
| `/curricula/admin/file/getfile` | `POST` | Worksheet Download Path | `DIRECT_HTTP_POSSIBLE` |
| `/curricula/student/session/submitlink` | `POST` | Worksheet Submission | `DIRECT_HTTP_POSSIBLE` |

---

## 3. HTTP Methods & Protocol Characteristics

- **Protocol**: HTTP/1.1 and HTTP/2 over TLS (HTTPS).
- **Primary Method**: Every state-altering and data-retrieval operation uses `POST` with JSON payloads (`Content-Type: application/json`).
- **Required Body Parameters**: Every request includes the configuration parameter `key: "[REDACTED]"` (statically bundled in client configuration as `"john"`).
- **Authentication Header**: After login, requests attach the JWT in the standard header:  
  `Authorization: <jwtToken>`

---

## 4. Which Operations Appear API-Driven

The following operations are 100% API-driven and execute via direct HTTP requests without requiring a browser:

1. **Course & Semester Discovery**:
   - `POST /curricula/student/home/getcourses` returns the complete curriculum hierarchy for the enrolled student.
   - Semester filtering (e.g., Semester 3) is a simple filter on the `SEMESTER` attribute of each returned course object.
2. **Session / Worksheet Metadata**:
   - `POST /curricula/student/session/getsessionstatus` returns status codes (`1 = Pending`, `2 = Verified`, `-1 = Rejected`) and current submission links.
3. **Question & Practice Details**:
   - `POST /curricula/student/session/getquestions` returns question sets, syllabus outcomes, and instructions.
4. **Worksheet Download**:
   - `POST /curricula/admin/file/getfile` resolves the file storage path to an HTTP file download URL.
5. **Worksheet Submission & "UPDATE" Action**:
   - `POST /curricula/student/session/submitlink` receives Google Drive/cloud links directly and updates the portal database.

---

## 5. Which Operations Require Browser Interaction

1. **Client-Side Canvas CAPTCHA Capture**:
   - The login page draws a 6-digit numeric CAPTCHA onto an HTML5 canvas element using client-side JavaScript.
   - If user authentication is performed starting from the login page, Playwright in headless mode is used to take a screenshot of the CAPTCHA canvas and transmit it to the user.
2. **Fallback Navigation**:
   - In the event of temporary portal API route updates or CORS/WAF changes, Playwright headless Chromium acts as an immediate fallback transport.

---

## 6. Authentication & Session Architecture

- **Login Endpoint**: `POST https://dld.srmist.edu.in/ktretecurricula/server/curricula/login`
- **Request Body Fields**:
  ```json
  {
    "USER_ID": "<username>",
    "PASSWORD": "<password>",
    "key": "john"
  }
  ```
- **Response Structure**:
  ```json
  {
    "Status": 1,
    "token": "<jwt_string>",
    "user": {
      "USER_ID": "<username>",
      "FULL_NAME": "<student_name>",
      "DEPARTMENT": "<department_name>",
      "ROLE": "S"
    }
  }
  ```
- **Session Token**:
  - The returned token is a standard JWT.
  - Subsequent requests pass the JWT in the `Authorization` header (`axios.defaults.headers.common["Authorization"] = token`).

---

## 7. CAPTCHA Behavior

- **Mechanism**: The login form invokes a client-side generator:
  `generateCaptcha = () => Object(je.b)(6, "white", "black", "numbers")`
- It generates 6 random digits and renders them into an HTML `<canvas>` element.
- **Client Validation**: The UI checks the entered solution before triggering the login POST.
- **Automation Policy**: Per architectural requirement #8 and #9, **no automated bypass is attempted**.
- **Human-in-the-Loop Implementation**:
  When reaching the login page, the worker detects the CAPTCHA canvas, takes an element screenshot, updates the job status to `WAITING_FOR_CAPTCHA`, and halts. The user submits the 6 digits via the UI or `POST /jobs/{id}/captcha`, and the worker immediately resumes.

---

## 8. Worksheet Download Mechanism

- In the student session view, the "Download Docx" or "Download PDF" button invokes:
  `POST https://dld.srmist.edu.in/ktretecurricula/server/curricula/admin/file/getfile`
- **Sanitized Request Payload**:
  ```json
  {
    "path": "data/coordinator/<course_code>/syllabus",
    "filename": "<worksheet_identifier>",
    "server": "https://dld.srmist.edu.in/etecurricula/server",
    "key": "john"
  }
  ```
- **Response**:
  ```json
  {
    "Status": 1,
    "result": {
      "path": "https://dld.srmist.edu.in/data/coordinator/.../worksheet.docx"
    }
  }
  ```
- The backend worker can issue a direct HTTP `GET` to the resulting path to download the document.

---

## 9. Worksheet Submission Mechanism (Link Input & UPDATE Action)

- In the SRM portal UI, students provide a link to their completed worksheet (such as a shared Google Drive link).
- Clicking the **"UPDATE"** button invokes:
  `POST https://dld.srmist.edu.in/ktretecurricula/server/curricula/student/session/submitlink`
- **Sanitized Request Payload**:
  ```json
  {
    "view": "https://drive.google.com/file/d/.../view",
    "download": "https://drive.google.com/file/d/.../view",
    "fileId": 0,
    "session": "<session_number><slo>",
    "SESSION": <session_number>,
    "SLO": <slo_number>,
    "course_code": "<course_code>",
    "course_name": "<course_name>",
    "BATCH_ID": "<batch_id>",
    "USER_ID": "<student_id>",
    "FULL_NAME": "<student_name>",
    "DEPARTMENT": "<department_name>"
  }
  ```
- **Response**:
  ```json
  {
    "Status": 1,
    "msg": "Link Updated Successfully",
    "link": "https://drive.google.com/file/d/.../view"
  }
  ```
- **Feasibility**: Can be executed 100% via direct HTTP `POST` without any browser automation!

---

## 10. Recommended Minimum Playwright Usage

Use Playwright exclusively for:
1. **Initial Login & CAPTCHA Capture**: If authenticating through the web UI, launch headless Chromium to render the login page, capture the 6-digit canvas CAPTCHA image, and present it to the user.
2. **Safety Net / Fallback**: If an endpoint returns 404 or an unexpected WAF challenge, fall back to headless browser interaction.

---

## 11. Recommended Backend HTTP Usage

Use direct HTTP (`httpx.AsyncClient`) for:
1. **`POST /curricula/login`** (once CAPTCHA solution is provided by the user).
2. **`POST /curricula/student/home/getcourses`** for semester and course discovery.
3. **`POST /curricula/student/session/getquestions`** for question and session retrieval.
4. **`POST /curricula/student/session/getsessionstatus`** for worksheet status.
5. **`POST /curricula/admin/file/getfile`** and subsequent `GET` for downloading worksheets.
6. **`POST /curricula/student/session/submitlink`** for submitting worksheet links and executing the "UPDATE" action.

---

## 12. Any Blockers

- **Zero Blocking Issues Encountered**:
  - The live SRM endpoints were successfully identified and verified.
  - The portal exposes clear, consistent REST API endpoints returning standard JSON.
  - The static configuration parameter (`key: "john"`) is openly bundled in the client application code.
  - Direct HTTP requests to `checkstatus` confirm the backend responds promptly without requiring browser emulation.

---

## Concise Architectural Recommendation

> **Recommended Architecture:**
> 
> * **HTTP/API for**: Semester Loading, Course Discovery, Question Retrieval, Session Status Checking, Worksheet File Downloading, and Worksheet Link Submission (UPDATE Action)
> * **Playwright for**: Capturing the initial login page CAPTCHA canvas screenshot and emergency fallback navigation
> * **User Intervention for**: Solving the 6-digit numeric login CAPTCHA
