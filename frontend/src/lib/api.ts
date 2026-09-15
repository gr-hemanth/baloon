/**
 * API Client for SRM eCurricula Automator Backend.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

export interface GoogleDriveStatus {
  connected: boolean;
  client_configured: boolean;
  has_refresh_token: boolean;
  redirect_uri: string;
}

export interface WorksheetItem {
  session: number;
  slo: number;
  filename: string;
  format: string;
  is_available: boolean;
  submission_status: string;
  title?: string;
  download_url?: string;
}

export interface CourseItem {
  course_code: string;
  course_name: string;
  batch_id: string;
  semester: number;
  department?: string;
  worksheets: WorksheetItem[];
}

export interface SRMDiscoverResponse {
  status: string;
  message?: string;
  semester: number;
  courses: CourseItem[];
  captcha_challenge?: any;
}

export interface JobResponse {
  id: string;
  user_id?: string;
  status: string;
  course_id?: string;
  semester_id?: string;
  worksheet_id?: string;
  transport_mode: string;
  current_step?: string;
  captcha_challenge?: any;
  result?: {
    course_code?: string;
    course_name?: string;
    session?: number;
    slo?: number;
    original_file?: string;
    completed_file?: string;
    drive_file_id?: string;
    drive_web_url?: string;
    drive_permission_status?: string;
    verification_status?: string;
    practice_status?: number;
    questions_count?: number;
    answers_count?: number;
  };
  error_message?: string;
  created_at: string;
  updated_at: string;
}

export async function checkDriveStatus(): Promise<GoogleDriveStatus> {
  const res = await fetch(`${API_BASE}/auth/google/status`);
  if (!res.ok) throw new Error("Failed to check Google Drive status");
  return res.json();
}

export async function getDriveAuthUrl(): Promise<{ authorization_url: string }> {
  const res = await fetch(`${API_BASE}/auth/google/url`);
  if (!res.ok) throw new Error("Failed to get Google Drive auth URL");
  return res.json();
}

export async function discoverSRMCourses(
  userId: string,
  pass: string,
  sem: number = 3,
  captchaSolution?: string,
  transportMode: string = "auto"
): Promise<SRMDiscoverResponse> {
  const res = await fetch(`${API_BASE}/srm/discover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      password: pass,
      semester: sem,
      captcha_solution: captchaSolution,
      transport_mode: transportMode,
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || "Authentication / Discovery failed");
  }
  return res.json();
}

export async function createJob(params: {
  user_id: string;
  course_id: string;
  semester_id: string;
  worksheet_id: string;
  transport_mode?: string;
  credentials: { USER_ID: string; PASSWORD: string };
}): Promise<JobResponse> {
  const res = await fetch(`${API_BASE}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (res.status === 409) {
    const data = await res.json().catch(() => ({}));
    const err: any = new Error(data.detail || "Duplicate job is already running");
    err.status = 409;
    err.existingJobId = res.headers.get("X-Existing-Job-Id") ||
      (typeof data.detail === "string" ? data.detail.match(/job '([^']+)'/)?.[1] : undefined);
    throw err;
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || "Job creation failed");
  }
  return res.json();
}

export async function getJob(jobId: string): Promise<JobResponse> {
  const res = await fetch(`${API_BASE}/jobs/${jobId}`);
  if (!res.ok) throw new Error("Failed to fetch job");
  return res.json();
}

export async function resumeJob(
  jobId: string,
  solution: string
): Promise<JobResponse> {
  const res = await fetch(`${API_BASE}/jobs/${jobId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ solution }),
  });
  if (!res.ok) {
    // Fallback to /captcha
    return submitCaptchaSolution(jobId, solution);
  }
  return res.json();
}

export async function submitCaptchaSolution(
  jobId: string,
  solution: string
): Promise<JobResponse> {
  const res = await fetch(`${API_BASE}/jobs/${jobId}/captcha`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ solution }),
  });
  if (!res.ok) throw new Error("Failed to submit CAPTCHA solution");
  return res.json();
}
