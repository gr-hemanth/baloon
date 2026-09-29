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
  worksheet_id?: string;
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

export interface SRMAuthStatus {
  phase: "IDLE" | "AUTHENTICATING" | "WAITING_FOR_CAPTCHA" | "AUTHENTICATED" | "DISCOVERING" | "SUCCESS" | "FAILED";
  message: string;
  user_id?: string;
  updated_at: string;
}

export async function getSRMAuthStatus(): Promise<SRMAuthStatus> {
  const res = await fetch(`${API_BASE}/srm/status`);
  if (!res.ok) throw new Error("Failed to fetch SRM auth status");
  return res.json();
}

export interface JobResponse {
  id: string;
  user_id?: string;
  status: string;
  course_id?: string;
  semester_id?: string;
  subject_id?: string;
  worksheet_id?: string;
  transport_mode: string;
  current_step?: string;
  captcha_challenge?: any;
  result?: {
    course_code?: string;
    course_name?: string;
    semester?: number;
    session?: number;
    slo?: number;
    batch_id?: string;
    original_file?: string;
    original_file_path?: string;
    completed_file?: string;
    completed_file_path?: string;
    drive_file_id?: string;
    drive_web_url?: string;
    drive_permission_status?: string;
    drive_verified?: boolean;
    review_ready?: boolean;
    submission_allowed?: boolean;
    verification_status?: string;
    practice_status?: number;
    questions_count?: number;
    answers_count?: number;
    transport_used?: string;
    review_ready_at?: string;
    submission_started_at?: string;
    submission_verified_at?: string;
  };
  error_message?: string;
  created_at: string;
  updated_at: string;
  review_ready?: boolean;
  completed_file_name?: string;
  drive_file_id?: string;
  drive_web_view_link?: string;
  drive_verified?: boolean;
  submission_allowed?: boolean;
  questions_count?: number;
  answers_count?: number;
  submission_started_at?: string;
  submission_verified_at?: string;
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
      solution: captchaSolution,
      transport_mode: transportMode,
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || "Authentication / Discovery failed");
  }
  return res.json();
}

export async function resumeSRMDiscovery(
  userId: string,
  pass: string,
  solution: string,
  sem: number = 3,
  transportMode: string = "auto"
): Promise<SRMDiscoverResponse> {
  let res = await fetch(`${API_BASE}/srm/discover/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      password: pass,
      semester: sem,
      captcha_solution: solution,
      solution: solution,
      transport_mode: transportMode,
    }),
  });
  if (!res.ok && res.status === 404) {
    res = await fetch(`${API_BASE}/srm/discover`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: userId,
        password: pass,
        semester: sem,
        captcha_solution: solution,
        solution: solution,
        transport_mode: transportMode,
      }),
    });
  }
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
  session?: number;
  slo?: number;
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

export async function listJobs(limit: number = 5): Promise<JobResponse[]> {
  const res = await fetch(`${API_BASE}/jobs?limit=${limit}`);
  if (!res.ok) throw new Error("Failed to list jobs");
  return res.json();
}

export async function submitJobToSrm(
  jobId: string,
  credentials?: Record<string, any>
): Promise<JobResponse> {
  const res = await fetch(`${API_BASE}/jobs/${jobId}/submit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ credentials }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || "Failed to submit job to SRM");
  }
  return res.json();
}

export async function cancelJob(jobId: string): Promise<JobResponse> {
  const res = await fetch(`${API_BASE}/jobs/${jobId}/cancel`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to cancel job");
  return res.json();
}

export function getJobDownloadUrl(jobId: string): string {
  return `${API_BASE}/jobs/${jobId}/download`;
}

export interface StatusUIInfo {
  label: string;
  description: string;
  badgeClass: string;
  dotClass: string;
  textClass: string;
}

export const STATUS_UI_MAP: Record<string, StatusUIInfo> = {
  PENDING: {
    label: "Queued",
    description: "Job received and enqueued in broker",
    badgeClass: "bg-slate-800 text-slate-300 border-slate-700",
    dotClass: "bg-slate-700 border-slate-500",
    textClass: "text-slate-400",
  },
  RUNNING: {
    label: "Processing",
    description: "Worker initialized, connecting to SRM portal",
    badgeClass: "bg-blue-500/10 text-blue-400 border-blue-500/20",
    dotClass: "bg-blue-500 border-white pulse-dot",
    textClass: "text-blue-300 font-semibold",
  },
  WAITING_FOR_CAPTCHA: {
    label: "CAPTCHA required",
    description: "Paused: Portal presented a CAPTCHA challenge",
    badgeClass: "bg-amber-500/10 text-amber-400 border-amber-500/20 pulse-dot",
    dotClass: "bg-amber-500 border-amber-300 pulse-dot",
    textClass: "text-amber-300 font-semibold",
  },
  DOWNLOADING: {
    label: "Downloading worksheet",
    description: "Downloading original worksheet document",
    badgeClass: "bg-blue-500/10 text-blue-400 border-blue-500/20",
    dotClass: "bg-blue-500 border-white pulse-dot",
    textClass: "text-blue-300 font-semibold",
  },
  PROCESSING: {
    label: "Generating/filling answers",
    description: "Parsing questions and generating verified answers",
    badgeClass: "bg-blue-500/10 text-blue-400 border-blue-500/20",
    dotClass: "bg-blue-500 border-white pulse-dot",
    textClass: "text-blue-300 font-semibold",
  },
  UPLOADING: {
    label: "Uploading to Drive",
    description: "Storing completed worksheet in Google Drive",
    badgeClass: "bg-blue-500/10 text-blue-400 border-blue-500/20",
    dotClass: "bg-blue-500 border-white pulse-dot",
    textClass: "text-blue-300 font-semibold",
  },
  AWAITING_USER_REVIEW: {
    label: "Waiting for your review/approval",
    description: "Your completed worksheet is ready for review.",
    badgeClass: "bg-amber-500/10 text-amber-400 border-amber-500/20 pulse-dot",
    dotClass: "bg-amber-500 border-amber-300 pulse-dot",
    textClass: "text-amber-300 font-semibold",
  },
  SUBMITTING: {
    label: "Submitting to SRM",
    description: "Submitting verified link to SRM portal",
    badgeClass: "bg-blue-500/10 text-blue-400 border-blue-500/20",
    dotClass: "bg-blue-500 border-white pulse-dot",
    textClass: "text-blue-300 font-semibold",
  },
  VERIFYING: {
    label: "Verifying submission",
    description: "Confirming portal practice status update",
    badgeClass: "bg-blue-500/10 text-blue-400 border-blue-500/20",
    dotClass: "bg-blue-500 border-white pulse-dot",
    textClass: "text-blue-300 font-semibold",
  },
  COMPLETED: {
    label: "Completed",
    description: "Successfully finished and verified on portal",
    badgeClass: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
    dotClass: "bg-emerald-500 border-emerald-400",
    textClass: "text-emerald-400 font-semibold",
  },
  FAILED: {
    label: "Failed",
    description: "Job failed or was cancelled",
    badgeClass: "bg-red-500/10 text-red-400 border-red-500/20",
    dotClass: "bg-red-600 border-red-400",
    textClass: "text-red-400 font-semibold",
  },
};

export function getStatusUI(status?: string): StatusUIInfo {
  if (status && STATUS_UI_MAP[status]) {
    return STATUS_UI_MAP[status];
  }
  return {
    label: status ? status.replace(/_/g, " ") : "Unknown",
    description: status ? `State: ${status}` : "Status unavailable",
    badgeClass: "bg-slate-800 text-slate-400 border-slate-700",
    dotClass: "bg-slate-700 border-slate-600",
    textClass: "text-slate-400",
  };
}
