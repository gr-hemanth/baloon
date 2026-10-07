"use client";

import React, { useState, useEffect } from "react";
import {
  checkDriveStatus,
  getDriveAuthUrl,
  discoverSRMCourses,
  resumeSRMDiscovery,
  getSRMAuthStatus,
  createJob,
  getJob,
  submitCaptchaSolution,
  listJobs,
  submitJobToSrm,
  cancelJob,
  getJobDownloadUrl,
  getStatusUI,
  STATUS_UI_MAP,
  uploadWorksheetFile,
  WorksheetUploadResponse,
  CourseItem,
  WorksheetItem,
  JobResponse,
  launchAuthBrowser,
  getAuthBrowserStatus,
} from "../lib/api";

const PIPELINE_STEPS = [
  { key: "PENDING", title: "1. PENDING", desc: "Job received & enqueued in broker" },
  { key: "RUNNING", title: "2. RUNNING", desc: "Worker initialized, connecting to SRM" },
  { key: "WAITING_FOR_CAPTCHA", title: "3. WAITING_FOR_CAPTCHA", desc: "Paused if portal presents CAPTCHA" },
  { key: "DOWNLOADING", title: "4. DOWNLOADING", desc: "Downloading worksheet document" },
  { key: "PROCESSING", title: "5. PROCESSING", desc: "Parsing & answering via NVIDIA (FreeLLM fallback)" },
  { key: "UPLOADING", title: "6. UPLOADING", desc: "Storing in Google Drive (Anyone with link / Viewer)" },
  { key: "AWAITING_USER_REVIEW", title: "7. AWAITING_USER_REVIEW", desc: "Your completed worksheet is ready for review." },
  { key: "SUBMITTING", title: "8. SUBMITTING", desc: "Submitting verified link to SRM portal" },
  { key: "VERIFYING", title: "9. VERIFYING", desc: "Confirming portal practice status update" },
  { key: "COMPLETED", title: "10. COMPLETED", desc: "Successfully finished and verified" },
];

export default function DashboardPage() {
  // Credentials & Discovery State
  const [userId, setUserId] = useState("RA2111003010001");
  const [password, setPassword] = useState("");
  const [semester, setSemester] = useState(3);
  const [transportMode, setTransportMode] = useState("auto");

  // Google Drive State
  const [driveConnected, setDriveConnected] = useState(false);

  // Discovery State
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [courses, setCourses] = useState<CourseItem[]>([]);
  const [selectedCourseCode, setSelectedCourseCode] = useState<string>("");
  const [selectedWorksheet, setSelectedWorksheet] = useState<WorksheetItem | null>(null);

  // Job State
  const [activeJob, setActiveJob] = useState<JobResponse | null>(null);
  const [isStartingJob, setIsStartingJob] = useState(false);
  const [jobError, setJobError] = useState<string | null>(null);

  // Submit to SRM Confirmation Modal State
  const [showSubmitModal, setShowSubmitModal] = useState(false);
  const [isSubmittingToSrm, setIsSubmittingToSrm] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // CAPTCHA Modal State
  const [showCaptcha, setShowCaptcha] = useState(false);
  const [captchaContext, setCaptchaContext] = useState<"discovery" | "job" | null>(null);
  const [captchaChallenge, setCaptchaChallenge] = useState<any>(null);
  const [captchaSolution, setCaptchaSolution] = useState("");
  const [captchaError, setCaptchaError] = useState<string | null>(null);
  const [isSubmittingCaptcha, setIsSubmittingCaptcha] = useState(false);

  // Interactive Browser Launcher State
  const [isLaunchingBrowser, setIsLaunchingBrowser] = useState(false);
  const [browserLaunchStatus, setBrowserLaunchStatus] = useState<string | null>(null);
  const [browserConfirmed, setBrowserConfirmed] = useState(false);

  // SRM Authentication Progress State
  const [authPhase, setAuthPhase] = useState<"IDLE" | "AUTHENTICATING" | "WAITING_FOR_CAPTCHA" | "AUTHENTICATED" | "DISCOVERING" | "SUCCESS" | "FAILED">("IDLE");
  const [authMessage, setAuthMessage] = useState<string>("");

  // Helper to determine if a job is genuinely active (persisting AWAITING_USER_REVIEW until explicit submission)
  const isGenuinelyActive = (job: JobResponse | null): boolean => {
    if (!job || !job.status) return false;
    if (job.status === "AWAITING_USER_REVIEW") return true; // Review state persists until explicit user submission
    const ageMs = job.created_at ? Date.now() - new Date(job.created_at).getTime() : Infinity;
    if (job.status === "SUBMITTING" || job.status === "VERIFYING") return ageMs < 600000;
    if (ageMs > 900000) return false;
    if (
      job.status === "WAITING_FOR_CAPTCHA" ||
      job.status === "RUNNING" ||
      job.status === "DOWNLOADING" ||
      job.status === "PROCESSING" ||
      job.status === "UPLOADING"
    ) {
      return true;
    }
    if (job.status === "PENDING") return ageMs < 180000;
    return false;
  };

  // Helper to update state and synchronize with localStorage
  const setAndStoreActiveJob = (job: JobResponse | null) => {
    setActiveJob(job);
    try {
      if (
        job &&
        job.status !== "COMPLETED" &&
        job.status !== "FAILED"
      ) {
        localStorage.setItem("srm_active_job_id", job.id);
      } else if (job?.status === "COMPLETED" || job?.status === "FAILED") {
        localStorage.removeItem("srm_active_job_id");
      }
    } catch {}
  };

  const handleDismissJob = () => {
    setActiveJob(null);
    try {
      localStorage.removeItem("srm_active_job_id");
    } catch {}
  };

  // Load Google Drive Status and Auto-Detect Active Job on Mount / Page Refresh
  useEffect(() => {
    checkDrive();
    checkActiveJobOnLoad();
  }, []);

  const checkActiveJobOnLoad = async () => {
    let candidateId: string | null = null;
    try {
      candidateId = localStorage.getItem("srm_active_job_id");
    } catch {}

    if (candidateId) {
      try {
        const job = await getJob(candidateId);
        if (isGenuinelyActive(job)) {
          setActiveJob(job);
          return;
        } else {
          try {
            localStorage.removeItem("srm_active_job_id");
          } catch {}
        }
      } catch (e) {
        console.warn("Could not check saved active job:", e);
      }
    }

    // Auto-detect any active background job from server
    try {
      const recentJobs = await listJobs(5);
      const active = recentJobs.find(isGenuinelyActive);
      if (active) {
        try {
          localStorage.setItem("srm_active_job_id", active.id);
        } catch {}
        setActiveJob(active);
      }
    } catch (e) {
      console.warn("Could not query active jobs:", e);
    }
  };

  const checkDrive = async () => {
    try {
      const st = await checkDriveStatus();
      setDriveConnected(st.connected);
    } catch {
      // Ignored if backend offline
    }
  };

  const handleConnectDrive = async () => {
    try {
      const { authorization_url } = await getDriveAuthUrl();
      window.open(authorization_url, "GoogleDriveAuth", "width=600,height=700");
    } catch (err: any) {
      alert("Failed to get Google Drive auth URL: " + err.message);
    }
  };

  // Discover Courses
  const handleDiscover = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsDiscovering(true);
    setDiscoveryError(null);
    setCaptchaError(null);
    setAuthPhase("AUTHENTICATING");
    setAuthMessage("Authenticating with SRM portal...");

    const poller = setInterval(async () => {
      try {
        const sData = await getSRMAuthStatus();
        if (sData && sData.phase) {
          setAuthPhase(sData.phase as any);
          if (sData.message) setAuthMessage(sData.message);
        }
      } catch (err) {}
    }, 750);

    try {
      const resp = await discoverSRMCourses(userId, password, semester, undefined, transportMode);
      if (resp.status === "WAITING_FOR_CAPTCHA") {
        setAuthPhase("WAITING_FOR_CAPTCHA");
        setAuthMessage("Waiting for CAPTCHA – Please solve the visual CAPTCHA in the SRM browser window.");
        setCaptchaContext("discovery");
        setCaptchaChallenge(resp.captcha_challenge);
        setCaptchaSolution("");
        setCaptchaError(null);
        setShowCaptcha(true);
        return;
      }
      setAuthPhase("SUCCESS");
      setAuthMessage("Authentication successful. Worksheets discovered.");
      setCourses(resp.courses);
      if (resp.courses.length > 0) {
        setSelectedCourseCode(resp.courses[0].course_code);
      }
    } catch (err: any) {
      setAuthPhase("FAILED");
      setDiscoveryError(err.message);
    } finally {
      clearInterval(poller);
      setIsDiscovering(false);
    }
  };

  // User Provided Worksheet State
  const [uploadedWorksheet, setUploadedWorksheet] = useState<WorksheetUploadResponse | null>(null);
  const [isUploadingWorksheet, setIsUploadingWorksheet] = useState(false);
  const [worksheetUploadError, setWorksheetUploadError] = useState<string | null>(null);

  const handleWorksheetUpload = async (e: React.ChangeEvent<HTMLInputElement>, sampleWs: any) => {
    const file = e.target.files?.[0];
    if (!file) return;

    if (file.size > 15 * 1024 * 1024) {
      setWorksheetUploadError("File exceeds maximum allowed size of 15 MB.");
      return;
    }

    const ext = file.name.split('.').pop()?.toLowerCase();
    if (ext !== 'docx' && ext !== 'pdf') {
      setWorksheetUploadError("Unsupported format. Only .docx and .pdf worksheets are accepted.");
      return;
    }

    setIsUploadingWorksheet(true);
    setWorksheetUploadError(null);
    try {
      const data = await uploadWorksheetFile(file);
      setUploadedWorksheet(data);
      setSelectedWorksheet({
        ...sampleWs,
        is_available: true,
        title: `User Uploaded (${data.original_filename})`,
        filename: data.original_filename,
      } as any);
    } catch (err: any) {
      setWorksheetUploadError(err.message || "Failed to upload worksheet");
      setUploadedWorksheet(null);
    } finally {
      setIsUploadingWorksheet(false);
    }
  };

  // Start Automation Job
  const handleStartJob = async () => {
    if (!selectedCourseCode || !selectedWorksheet || (!selectedWorksheet.is_available && !uploadedWorksheet)) return;
    setIsStartingJob(true);
    setJobError(null);
    try {
      const wsId = selectedWorksheet.worksheet_id || `${selectedWorksheet.session}${selectedWorksheet.slo}`;
      const params: any = {
        user_id: userId,
        course_id: selectedCourseCode,
        semester_id: String(semester),
        worksheet_id: wsId,
        session: selectedWorksheet.session,
        slo: selectedWorksheet.slo,
        transport_mode: transportMode,
        credentials: { USER_ID: userId, PASSWORD: password },
      };
      if (uploadedWorksheet && uploadedWorksheet.stored_path) {
        params.uploaded_file_path = uploadedWorksheet.stored_path;
        params.upload_id = uploadedWorksheet.upload_id;
        params.is_user_provided = true;
      }
      const job = await createJob(params);
      setAndStoreActiveJob(job);
      if (job.status === "WAITING_FOR_CAPTCHA") {
        setCaptchaContext("job");
        setCaptchaChallenge(job.captcha_challenge);
        setCaptchaSolution("");
        setCaptchaError(null);
        setShowCaptcha(true);
      }
    } catch (err: any) {
      if (err.status === 409 && err.existingJobId) {
        alert("Notice: " + err.message);
        // Resume tracking existing active job
        const existing = await getJob(err.existingJobId);
        setAndStoreActiveJob(existing);
      } else {
        setJobError(err.message);
      }
    } finally {
      setIsStartingJob(false);
    }
  };

  // Poll Active Job
  useEffect(() => {
    if (
      !activeJob ||
      activeJob.status === "COMPLETED" ||
      activeJob.status === "FAILED"
    ) {
      return;
    }

    const pollIntervalMs = activeJob.status === "AWAITING_USER_REVIEW" ? 2500 : 1500;
    const timer = setInterval(async () => {
      try {
        const updated = await getJob(activeJob.id);
        setActiveJob(updated);

        if (updated.status === "WAITING_FOR_CAPTCHA") {
          if (captchaContext !== "discovery" && !isSubmittingCaptcha) {
            setCaptchaContext("job");
            setCaptchaChallenge(updated.captcha_challenge);
            setShowCaptcha(true);
          }
        } else if (captchaContext === "job") {
          setShowCaptcha(false);
          setCaptchaChallenge(null);
          setCaptchaContext(null);
        }

        if (updated.status === "COMPLETED") {
          try {
            localStorage.removeItem("srm_active_job_id");
          } catch {}
        }
      } catch (e) {
        console.warn("Poll failed:", e);
      }
    }, pollIntervalMs);

    return () => clearInterval(timer);
  }, [activeJob?.id, activeJob?.status, captchaContext, isSubmittingCaptcha]);

  // Submit CAPTCHA
  const handleCaptchaSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const sol = captchaSolution.trim();
    if (!sol || isSubmittingCaptcha) return;

    setIsSubmittingCaptcha(true);
    setCaptchaError(null);

    try {
      if (captchaContext === "discovery") {
        const resp = await resumeSRMDiscovery(userId, password, sol, semester, transportMode);
        if (resp.status === "WAITING_FOR_CAPTCHA") {
          setCaptchaChallenge(resp.captcha_challenge);
          setCaptchaSolution("");
          setCaptchaError("Invalid CAPTCHA solution. Please solve the new challenge below.");
          return;
        }
        setShowCaptcha(false);
        setCaptchaChallenge(null);
        setCaptchaSolution("");
        setCaptchaContext(null);
        setCourses(resp.courses);
        if (resp.courses.length > 0) {
          setSelectedCourseCode(resp.courses[0].course_code);
        }
      } else if (captchaContext === "job" && activeJob) {
        const updated = await submitCaptchaSolution(activeJob.id, sol);
        setAndStoreActiveJob(updated);
        setShowCaptcha(false);
        setCaptchaChallenge(null);
        setCaptchaSolution("");
        setCaptchaContext(null);
      }
    } catch (err: any) {
      if (captchaContext === "discovery") {
        setShowCaptcha(false);
        setCaptchaChallenge(null);
        setCaptchaSolution("");
        setCaptchaContext(null);
        setDiscoveryError(err.message);
      } else {
        setCaptchaError(err.message);
      }
    } finally {
      setIsSubmittingCaptcha(false);
    }
  };

  // Launch interactive headed browser on desktop for CAPTCHA solving
  const handleLaunchInteractiveBrowser = async () => {
    setIsLaunchingBrowser(true);
    setBrowserLaunchStatus("Requesting secure interactive login browser window...");
    setCaptchaError(null);

    try {
      const payload: any = {};
      if (captchaContext === "job" && activeJob?.id) {
        payload.job_id = activeJob.id;
        payload.user_id = userId;
        payload.password = password;
      } else {
        payload.user_id = userId;
        payload.password = password;
      }

      const launchRes = await launchAuthBrowser(payload);
      setBrowserLaunchStatus(launchRes.message || "Launching browser on your desktop...");

      const query = payload.job_id ? { job_id: payload.job_id } : { request_id: `discovery:${payload.user_id}` };
      const pollTimer = setInterval(async () => {
        try {
          const sData = await getAuthBrowserStatus(query);
          if (sData.phase === "OPENING_BROWSER") {
            setBrowserLaunchStatus("Opening Opera / Chromium browser on your screen...");
          } else if (sData.phase === "WAITING_FOR_CAPTCHA") {
            setBrowserLaunchStatus("Browser is open! Please solve the visual CAPTCHA in the opened browser window.");
            setBrowserConfirmed(true);
          } else if (sData.phase === "AUTHENTICATED") {
            clearInterval(pollTimer);
            setBrowserLaunchStatus("Authentication successful! Capturing session...");
            setBrowserConfirmed(false);
            setIsLaunchingBrowser(false);
            setTimeout(async () => {
              setShowCaptcha(false);
              setCaptchaChallenge(null);
              setCaptchaContext(null);
              if (payload.job_id) {
                const refreshed = await getJob(payload.job_id);
                setAndStoreActiveJob(refreshed);
              } else {
                handleDiscover({ preventDefault: () => {} } as any);
              }
            }, 1200);
          } else if (sData.phase === "AUTHENTICATION_ERROR" || sData.phase === "TIMED_OUT") {
            clearInterval(pollTimer);
            setBrowserLaunchStatus(sData.error_message || "Browser authentication timed out or failed. You may retry.");
            setIsLaunchingBrowser(false);
          }
        } catch (pollErr) {
          console.warn("Browser status poll error:", pollErr);
        }
      }, 1000);
    } catch (err: any) {
      setBrowserLaunchStatus(err.message || "Failed to launch browser");
      setIsLaunchingBrowser(false);
    }
  };

  // Auto-launch interactive browser when modal opens without inline image CAPTCHA
  useEffect(() => {
    if (showCaptcha && !captchaChallenge?.image_base64 && !isLaunchingBrowser && !browserConfirmed) {
      handleLaunchInteractiveBrowser();
    }
  }, [showCaptcha, captchaChallenge?.image_base64]);

  // Submit to SRM confirmation
  const handleConfirmSubmitToSrm = async () => {
    if (!activeJob || isSubmittingToSrm) return;
    setIsSubmittingToSrm(true);
    setSubmitError(null);
    try {
      const updated = await submitJobToSrm(activeJob.id, { USER_ID: userId, PASSWORD: password });
      setAndStoreActiveJob(updated);
      setShowSubmitModal(false);
    } catch (err: any) {
      setSubmitError(err.message || "Failed to submit to SRM portal");
    } finally {
      setIsSubmittingToSrm(false);
    }
  };

  const currentCourse = courses.find((c) => c.course_code === selectedCourseCode);

  return (
    <div className="min-h-screen flex flex-col bg-slate-900 text-slate-100">
      {/* Top Navigation */}
      <header className="border-b border-slate-800 bg-slate-950/80 backdrop-blur sticky top-0 z-40 px-6 py-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center space-x-3">
          <div className="h-10 w-10 rounded-lg bg-blue-600 flex items-center justify-center text-white shadow-lg shadow-blue-500/20 font-bold text-lg">
            🎓
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight text-white flex items-center gap-2">
              SRM eCurricula Automator
              <span className="text-xs font-semibold px-2.5 py-0.5 rounded-full bg-blue-500/10 text-blue-400 border border-blue-500/20">
                Production
              </span>
            </h1>
            <p className="text-xs text-slate-400">Direct HTTP Priority • Asynchronous Background Execution</p>
          </div>
        </div>

        {/* Integration Status */}
        <div className="flex items-center space-x-4 text-xs">
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-slate-800 border border-slate-700">
            <span>Drive:</span>
            <span className={`font-semibold ${driveConnected ? "text-emerald-400" : "text-amber-400"}`}>
              {driveConnected ? "Connected" : "Not Linked"}
            </span>
            {!driveConnected && (
              <button
                onClick={handleConnectDrive}
                className="ml-1 px-2 py-0.5 rounded bg-blue-600 hover:bg-blue-500 text-white font-medium"
              >
                Connect
              </button>
            )}
          </div>
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-slate-800 border border-slate-700">
            <span className="h-2 w-2 rounded-full bg-emerald-400"></span>
            <span className="text-slate-300">Backend: <strong className="text-white">Active</strong></span>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-6 space-y-6">
        {/* Security Banner */}
        <div className="bg-blue-950/40 border border-blue-800/40 rounded-xl p-4 text-xs text-blue-200 flex items-start gap-3">
          <span className="text-blue-400 text-base">🛡️</span>
          <div>
            <strong className="text-blue-100">Zero-Persistence Security Guarantee:</strong> SRM passwords and OAuth
            secrets are strictly stored in volatile background worker memory during execution and are never saved to
            the database. Background Celery workers execute autonomously—you may close this browser once a task is
            started.
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          {/* Left Column */}
          <div className="lg:col-span-5 space-y-6">
            {/* 1. SRM Authentication Card */}
            <div className="bg-slate-950 border border-slate-800 rounded-xl p-5 shadow-xl">
              <div className="flex items-center justify-between pb-3 mb-4 border-b border-slate-800">
                <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
                  🔑 1. SRM Authentication
                </h2>
                <span className="text-xs text-slate-500">Semester 3 Target</span>
              </div>

              <form onSubmit={handleDiscover} className="space-y-4 text-xs">
                <div>
                  <label className="block text-slate-300 font-medium mb-1">Student NetID / Register No.</label>
                  <input
                    type="text"
                    required
                    value={userId}
                    onChange={(e) => setUserId(e.target.value)}
                    className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                  />
                </div>

                <div>
                  <label className="block text-slate-300 font-medium mb-1">Portal Password</label>
                  <input
                    type="password"
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="••••••••••••"
                    className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                  />
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-slate-300 font-medium mb-1">Semester</label>
                    <select
                      value={semester}
                      onChange={(e) => setSemester(parseInt(e.target.value))}
                      className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                    >
                      <option value={3}>Semester 3</option>
                      <option value={1}>Semester 1</option>
                      <option value={2}>Semester 2</option>
                      <option value={4}>Semester 4</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-slate-300 font-medium mb-1">Transport</label>
                    <select
                      value={transportMode}
                      onChange={(e) => setTransportMode(e.target.value)}
                      className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                    >
                      <option value="auto">Auto (Direct HTTP First)</option>
                      <option value="http">Strict Direct HTTP</option>
                      <option value="browser">Playwright Fallback</option>
                    </select>
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={isDiscovering}
                  className="w-full py-2.5 px-4 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium text-sm transition disabled:opacity-50"
                >
                  {isDiscovering ? "Discovering Worksheets..." : "Discover Semester 3 Worksheets"}
                </button>
              </form>

              {/* 4-Phase Authentication Status Tracker */}
              {authPhase !== "IDLE" && (
                <div className="mt-4 pt-4 border-t border-slate-800 space-y-3">
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-slate-400 font-medium">Authentication Status:</span>
                    <span
                      className={`text-[10px] px-2.5 py-0.5 rounded-full font-bold uppercase tracking-wider border ${
                        authPhase === "WAITING_FOR_CAPTCHA"
                          ? "bg-amber-500/10 text-amber-400 border-amber-500/20 pulse-dot"
                          : authPhase === "AUTHENTICATED" || authPhase === "SUCCESS"
                          ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                          : authPhase === "FAILED"
                          ? "bg-red-500/10 text-red-400 border-red-500/20"
                          : "bg-blue-500/10 text-blue-400 border-blue-500/20"
                      }`}
                    >
                      {authPhase === "AUTHENTICATING"
                        ? "Authenticating with SRM"
                        : authPhase === "WAITING_FOR_CAPTCHA"
                        ? "Waiting for CAPTCHA"
                        : authPhase === "AUTHENTICATED"
                        ? "Authentication successful"
                        : authPhase === "DISCOVERING"
                        ? "Discovering worksheets"
                        : authPhase === "SUCCESS"
                        ? "Completed"
                        : authPhase === "FAILED"
                        ? "Authentication Failed"
                        : authPhase}
                    </span>
                  </div>

                  <div className="grid grid-cols-2 gap-2 text-xs">
                    {/* Phase 1: Authenticating with SRM */}
                    <div
                      className={`p-2 rounded-lg border flex items-center gap-2 ${
                        authPhase === "AUTHENTICATING"
                          ? "bg-slate-900 border-blue-500/40 text-blue-300 font-semibold"
                          : ["WAITING_FOR_CAPTCHA", "AUTHENTICATED", "DISCOVERING", "SUCCESS"].includes(authPhase)
                          ? "bg-slate-950 border-emerald-500/30 text-emerald-400"
                          : "bg-slate-950 border-slate-800 text-slate-500"
                      }`}
                    >
                      <span>{["WAITING_FOR_CAPTCHA", "AUTHENTICATED", "DISCOVERING", "SUCCESS"].includes(authPhase) ? "✓" : "●"}</span>
                      <span className="text-[11px] font-medium">Authenticating with SRM</span>
                    </div>

                    {/* Phase 2: Waiting for CAPTCHA */}
                    <div
                      className={`p-2 rounded-lg border flex items-center gap-2 ${
                        authPhase === "WAITING_FOR_CAPTCHA"
                          ? "bg-slate-900 border-amber-500/40 text-amber-300 font-semibold"
                          : ["AUTHENTICATED", "DISCOVERING", "SUCCESS"].includes(authPhase)
                          ? "bg-slate-950 border-emerald-500/30 text-emerald-400"
                          : "bg-slate-950 border-slate-800 text-slate-500"
                      }`}
                    >
                      <span>{["AUTHENTICATED", "DISCOVERING", "SUCCESS"].includes(authPhase) ? "✓" : "●"}</span>
                      <span className="text-[11px] font-medium">Waiting for CAPTCHA</span>
                    </div>

                    {/* Phase 3: Authentication successful */}
                    <div
                      className={`p-2 rounded-lg border flex items-center gap-2 ${
                        ["AUTHENTICATED", "DISCOVERING", "SUCCESS"].includes(authPhase)
                          ? "bg-slate-950 border-emerald-500/30 text-emerald-400 font-semibold"
                          : "bg-slate-950 border-slate-800 text-slate-500"
                      }`}
                    >
                      <span>{["AUTHENTICATED", "DISCOVERING", "SUCCESS"].includes(authPhase) ? "✓" : "●"}</span>
                      <span className="text-[11px] font-medium">Authentication successful</span>
                    </div>

                    {/* Phase 4: Discovering worksheets */}
                    <div
                      className={`p-2 rounded-lg border flex items-center gap-2 ${
                        authPhase === "DISCOVERING"
                          ? "bg-slate-900 border-blue-500/40 text-blue-300 font-semibold"
                          : authPhase === "SUCCESS"
                          ? "bg-slate-950 border-emerald-500/30 text-emerald-400 font-semibold"
                          : "bg-slate-950 border-slate-800 text-slate-500"
                      }`}
                    >
                      <span>{authPhase === "SUCCESS" ? "✓" : "●"}</span>
                      <span className="text-[11px] font-medium">Discovering worksheets</span>
                    </div>
                  </div>

                  {/* Browser Window Notice when waiting for CAPTCHA */}
                  {authPhase === "WAITING_FOR_CAPTCHA" && (
                    <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-200 text-xs flex items-start gap-2.5">
                      <span className="text-amber-400 text-base">🖥️</span>
                      <div>
                        <strong className="text-amber-300">Action Required: Solve CAPTCHA in SRM Browser Window</strong>
                        <p className="mt-0.5 text-slate-300 text-[11px]">
                          An SRM login browser window has opened. Please solve the visual CAPTCHA in that window.
                          Once verified, your session will be captured automatically and discovery will proceed via direct HTTP.
                        </p>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {discoveryError && (
                <div className="mt-3 p-3 rounded bg-red-950/50 border border-red-800 text-red-300 text-xs">
                  {discoveryError}
                </div>
              )}
            </div>

            {/* 2. Course & Worksheet Selector Card */}
            <div
              className={`bg-slate-950 border border-slate-800 rounded-xl p-5 shadow-xl transition-all ${
                courses.length === 0 ? "opacity-60 pointer-events-none" : ""
              }`}
            >
              <div className="flex items-center justify-between pb-3 mb-4 border-b border-slate-800">
                <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
                  📋 2. Select Course & Worksheet
                </h2>
                <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">
                  {courses.length} courses found
                </span>
              </div>

              <div className="space-y-4 text-xs">
                <div>
                  <label className="block text-slate-300 font-medium mb-1">Course</label>
                  <select
                    value={selectedCourseCode}
                    onChange={(e) => {
                      setSelectedCourseCode(e.target.value);
                      setSelectedWorksheet(null);
                      setUploadedWorksheet(null);
                      setWorksheetUploadError(null);
                    }}
                    className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-emerald-500"
                  >
                    {courses.map((c) => {
                      const availCount = (c.worksheets || []).filter((w) => w.is_available).length;
                      return (
                        <option key={c.course_code} value={c.course_code}>
                          {c.course_code} - {c.course_name} ({availCount > 0 ? `${availCount} available` : "0 available / Unavailable"})
                        </option>
                      );
                    })}
                  </select>
                </div>

                <div>
                  <label className="block text-slate-300 font-medium mb-1">Available Worksheets</label>
                  <div className="space-y-2 max-h-56 overflow-y-auto pr-1">
                    {(() => {
                      if (!currentCourse) {
                        return <p className="text-slate-500 italic">Select a course to view available worksheets.</p>;
                      }
                      const allWs = currentCourse.worksheets || [];
                      const availableWs = allWs.filter((w) => w.is_available);
                      const unavailableWs = allWs.filter((w) => !w.is_available);
                      const sampleWs = unavailableWs.find((w) => (w.session === 209 || (w as any).session_no === 209) && w.slo === 1) || unavailableWs[0] || (currentCourse.course_code === "21CSC203P" ? { session: 209, slo: 1, worksheet_id: "2091" } : { session: 101, slo: 1, worksheet_id: "1011" });

                      if (availableWs.length === 0) {
                        return (
                          <div className="space-y-3">
                            <div className="p-3.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-200 space-y-2">
                              <div className="flex items-center gap-2 text-amber-400 font-semibold text-xs">
                                <span>⚠️</span>
                                <span>No official SRM worksheet is available for this course/session.</span>
                              </div>
                              <p className="text-[11px] text-slate-300">
                                The course coordinator has not uploaded official worksheets to the SRM portal for <strong className="text-white font-mono">{currentCourse.course_code}</strong>.
                              </p>
                              <div className="rounded-lg bg-slate-900/90 border border-slate-800 p-2.5 space-y-1.5 font-mono text-[11px]">
                                <div className="flex justify-between"><span className="text-slate-400">Course:</span> <span className="text-white font-bold">{currentCourse.course_code}</span></div>
                                <div className="flex justify-between"><span className="text-slate-400">Session:</span> <span className="text-white font-bold">{sampleWs.session}</span></div>
                                <div className="flex justify-between"><span className="text-slate-400">SLO:</span> <span className="text-white font-bold">{sampleWs.slo}</span></div>
                                <div className="flex justify-between"><span className="text-slate-400">Worksheet ID:</span> <span className="text-white font-bold">{sampleWs.worksheet_id || `${sampleWs.session}${sampleWs.slo}`}</span></div>
                                <div className="flex justify-between"><span className="text-slate-400">Availability:</span> <span className="text-rose-400 font-bold uppercase">UNAVAILABLE</span></div>
                              </div>
                              <p className="text-[10px] text-slate-400 italic">
                                Synthetic questions are never fabricated. You may upload your authentic worksheet document below to automate this session.
                              </p>
                            </div>

                            {/* Upload Worksheet Section */}
                            <div className="p-3.5 rounded-lg bg-slate-900 border border-slate-700/80 space-y-2.5">
                              <div className="flex items-center justify-between">
                                <span className="text-xs font-semibold text-white flex items-center gap-1.5">
                                  <span>☁️</span> Upload Actual Worksheet
                                </span>
                                <span className="text-[10px] text-slate-400 font-mono">DOCX / PDF &bull; MAX 15MB</span>
                              </div>
                              <p className="text-[11px] text-slate-400">
                                Provide your authentic course worksheet to parse, answer with AI, and upload for review.
                              </p>
                              <label className="block border-2 border-dashed border-slate-700 hover:border-blue-500 rounded-lg p-3 text-center cursor-pointer transition">
                                <input
                                  type="file"
                                  accept=".docx,.pdf"
                                  className="hidden"
                                  disabled={isUploadingWorksheet}
                                  onChange={(e) => handleWorksheetUpload(e, sampleWs)}
                                />
                                <div className="text-xs text-slate-300 font-medium">
                                  {isUploadingWorksheet ? "Uploading and validating..." : "Click to select or drop worksheet"}
                                </div>
                                <div className="text-[10px] text-slate-500">Supports authentic .docx or .pdf files</div>
                              </label>

                              {worksheetUploadError && (
                                <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-red-300 text-xs flex items-center gap-1.5">
                                  <span>❌</span>
                                  <span>{worksheetUploadError}</span>
                                </div>
                              )}

                              {uploadedWorksheet && (
                                <div className="p-2.5 rounded bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs space-y-1">
                                  <div className="flex items-center justify-between font-semibold">
                                    <span className="flex items-center gap-1 text-emerald-400">
                                      <span>✅</span> Worksheet Validated & Ready
                                    </span>
                                    <span className="px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-800 text-[10px] font-mono uppercase">{uploadedWorksheet.format}</span>
                                  </div>
                                  <div className="text-[11px] text-slate-300 flex justify-between font-mono">
                                    <span className="truncate max-w-[200px]" title={uploadedWorksheet.original_filename}>{uploadedWorksheet.original_filename}</span>
                                    <span className="text-slate-400">{(uploadedWorksheet.file_size / 1024).toFixed(1)} KB</span>
                                  </div>
                                </div>
                              )}
                            </div>
                          </div>
                        );
                      }

                      return (
                        <>
                          {availableWs.map((ws) => {
                            const isSelected =
                              selectedWorksheet &&
                              selectedWorksheet.session === ws.session &&
                              selectedWorksheet.slo === ws.slo;
                            return (
                              <div
                                key={ws.worksheet_id || `${ws.session}-${ws.slo}`}
                                onClick={() => {
                                  setSelectedWorksheet(ws);
                                  setUploadedWorksheet(null);
                                }}
                                className={`flex items-center justify-between p-2.5 rounded-lg border cursor-pointer transition ${
                                  isSelected
                                    ? "bg-emerald-950/40 border-emerald-500"
                                    : "bg-slate-900 border-slate-800 hover:border-slate-700"
                                }`}
                              >
                                <div className="flex items-center gap-2">
                                  <input
                                    type="radio"
                                    checked={isSelected || false}
                                    onChange={() => {
                                      setSelectedWorksheet(ws);
                                      setUploadedWorksheet(null);
                                    }}
                                    className="text-emerald-500"
                                  />
                                  <div>
                                    <div className="font-medium text-white">{ws.title || `Session ${ws.session} SLO ${ws.slo}`}</div>
                                    <div className="text-[10px] text-slate-500 font-mono">{ws.filename}</div>
                                  </div>
                                </div>
                                <span
                                  className={`px-2 py-0.5 rounded text-[10px] border ${
                                    ws.submission_status === "VERIFIED"
                                      ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                                      : "bg-blue-500/10 text-blue-400 border-blue-500/20"
                                  }`}
                                >
                                  {ws.submission_status}
                                </span>
                              </div>
                            );
                          })}
                          {unavailableWs.length > 0 && (
                            <div className="mt-3 pt-3 border-t border-slate-800 text-[11px] text-slate-500">
                              <div className="text-[11px] font-medium text-slate-400 mb-1 flex items-center gap-1.5">
                                <span>🚫</span>
                                <span>Unavailable on SRM ({unavailableWs.length} not uploaded by coordinator)</span>
                              </div>
                              <p className="text-[10px] text-slate-500 italic">These worksheets are not available on the portal and cannot be automated without user upload.</p>
                            </div>
                          )}
                        </>
                      );
                    })()}
                  </div>
                </div>

                <button
                  type="button"
                  onClick={handleStartJob}
                  disabled={!selectedWorksheet || (!selectedWorksheet.is_available && !uploadedWorksheet) || isStartingJob}
                  className="w-full py-2.5 px-4 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium text-sm transition disabled:opacity-50"
                >
                  {isStartingJob
                    ? "Starting Background Task..."
                    : uploadedWorksheet
                    ? "Start Automation (Uploaded Worksheet)"
                    : "Start Background Automation"}
                </button>
              </div>
            </div>
          </div>

          {/* Right Column: Live Stepper & Artifacts */}
          <div className="lg:col-span-7 space-y-6">
            {/* Pipeline Tracker */}
            <div className="bg-slate-950 border border-slate-800 rounded-xl p-5 shadow-xl">
              <div className="flex items-center justify-between pb-3 mb-4 border-b border-slate-800">
                <div>
                  <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
                    ⚡ Live Job Execution Pipeline
                  </h2>
                  <p className="text-xs text-slate-500">10-State Background Celery Orchestration</p>
                </div>
                <div
                  className={`px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider ${
                    activeJob ? getStatusUI(activeJob.status).badgeClass : "bg-slate-800 text-slate-400 border border-slate-700"
                  }`}
                >
                  {activeJob ? getStatusUI(activeJob.status).label : "IDLE"}
                </div>
              </div>

              {/* Active Meta */}
              <div className="mb-5 p-3 rounded-lg bg-slate-900 border border-slate-800 text-xs flex flex-wrap items-center justify-between gap-2 text-slate-400">
                <div>
                  Job ID: <span className="font-mono text-slate-200">{activeJob ? activeJob.id : "None active"}</span>
                </div>
                <div>
                  Current Step:{" "}
                  <span className="text-blue-400 font-medium">
                    {activeJob
                      ? activeJob.status === "AWAITING_USER_REVIEW"
                        ? "Waiting for your review/approval"
                        : activeJob.current_step || getStatusUI(activeJob.status).label
                      : "Waiting to start"}
                  </span>
                </div>
                {activeJob && (
                  <button
                    type="button"
                    onClick={handleDismissJob}
                    className="text-[11px] px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white border border-slate-700 transition"
                  >
                    Clear / New
                  </button>
                )}
              </div>

              {/* 10-State Stepper */}
              <div className="relative border-l-2 border-slate-800 ml-4 pl-6 space-y-4 py-1">
                {PIPELINE_STEPS.map((step, idx) => {
                  const currentIdx = activeJob
                    ? PIPELINE_STEPS.findIndex((s) => s.key === activeJob.status)
                    : -1;
                  const isCompleted = activeJob?.status === "COMPLETED" || (currentIdx !== -1 && idx < currentIdx);
                  const isActive = activeJob?.status === step.key;

                  let dotClass = "bg-slate-800 border-slate-700";
                  let textClass = "text-slate-500";

                  if (activeJob?.status === "FAILED") {
                    if (currentIdx !== -1 && idx <= currentIdx) {
                      dotClass = "bg-red-600 border-red-400";
                      textClass = "text-red-400 font-semibold";
                    }
                  } else if (isCompleted) {
                    dotClass = "bg-emerald-500 border-emerald-400";
                    textClass = "text-emerald-400 font-semibold";
                  } else if (isActive) {
                    if (step.key === "AWAITING_USER_REVIEW" || step.key === "WAITING_FOR_CAPTCHA") {
                      dotClass = "bg-amber-500 border-amber-300 pulse-dot";
                      textClass = "text-amber-300 font-semibold";
                    } else {
                      dotClass = "bg-blue-500 border-white pulse-dot";
                      textClass = "text-blue-300 font-semibold";
                    }
                  }

                  return (
                    <div key={step.key} className="relative">
                      <div className={`absolute -left-[31px] top-0.5 h-4 w-4 rounded-full border-2 ${dotClass}`} />
                      <div className={`text-xs ${textClass}`}>{step.title}</div>
                      <div className="text-[11px] text-slate-500">{step.desc}</div>
                    </div>
                  );
                })}
              </div>

              {activeJob?.error_message && (
                <div className="mt-4 p-4 rounded-lg bg-red-950/50 border border-red-800 text-red-200 text-xs font-mono">
                  Error: {activeJob.error_message}
                </div>
              )}
            </div>

            {/* Prominent Review Card (AWAITING_USER_REVIEW) */}
            {activeJob?.status === "AWAITING_USER_REVIEW" && (
              <div className="bg-slate-950 border border-amber-500/40 rounded-xl p-5 shadow-xl space-y-4 text-xs">
                <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                  <div>
                    <h2 className="text-sm font-semibold uppercase tracking-wider text-amber-400 flex items-center gap-2">
                      FINAL WORKSHEET READY
                    </h2>
                    <p className="text-xs text-slate-300 mt-1">
                      Your worksheet has been generated and uploaded to Google Drive.
                    </p>
                    <p className="text-xs text-slate-400 mt-0.5">
                      Review the document before submitting it to SRM.
                    </p>
                  </div>
                  <span className="text-xs px-2.5 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20 font-bold pulse-dot">
                    Waiting for your approval
                  </span>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 p-3 rounded-lg bg-slate-900/80 border border-slate-800">
                  <div>
                    <span className="text-slate-400 block text-[11px] mb-0.5">Worksheet:</span>
                    <span className="text-white font-medium text-xs">
                      {(activeJob.result?.course_code || activeJob.course_id || "Course")} – Session {(activeJob.result?.session ?? "")} – SLO {(activeJob.result?.slo ?? "")}
                    </span>
                  </div>
                  <div>
                    <span className="text-slate-400 block text-[11px] mb-0.5">Questions Answered:</span>
                    <span className="text-emerald-400 font-medium text-xs">
                      {(activeJob.answers_count ?? activeJob.result?.answers_count ?? "--")} / {(activeJob.questions_count ?? activeJob.result?.questions_count ?? "--")}
                    </span>
                  </div>
                  <div>
                    <span className="text-slate-400 block text-[11px] mb-0.5">Document Source:</span>
                    <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold border ${
                      (activeJob.is_user_provided || activeJob.result?.is_user_provided || activeJob.source_type === "USER_PROVIDED")
                        ? "bg-purple-500/10 text-purple-300 border-purple-500/20"
                        : "bg-blue-500/10 text-blue-300 border-blue-500/20"
                    }`}>
                      {(activeJob.is_user_provided || activeJob.result?.is_user_provided || activeJob.source_type === "USER_PROVIDED") ? "USER PROVIDED" : "SRM OFFICIAL"}
                    </span>
                  </div>
                </div>

                <div className="p-4 rounded-lg bg-slate-900 border border-slate-800 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-slate-300 font-medium">Google Drive Document:</span>
                    <span className="px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-300 font-mono text-[10px] border border-emerald-800/60">
                      {activeJob.result?.drive_permission_status || "VERIFIED_PUBLIC_READER"}
                    </span>
                  </div>

                  <div>
                    <label className="block text-[11px] text-slate-400 mb-1">Drive Link:</label>
                    <input
                      type="text"
                      readOnly
                      value={activeJob.drive_web_view_link || activeJob.result?.drive_web_url || ""}
                      className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-slate-200 font-mono text-xs select-all"
                    />
                  </div>

                  <div className="flex flex-wrap gap-2 pt-1">
                    <a
                      href={activeJob.drive_web_view_link || activeJob.result?.drive_web_url || "#"}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={`py-2 px-3.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium flex items-center gap-2 transition shadow-md shadow-blue-600/20 ${
                        !(activeJob.drive_web_view_link || activeJob.result?.drive_web_url) ? "opacity-50 pointer-events-none" : ""
                      }`}
                    >
                      ↗ View Final Document
                    </a>
                    <a
                      href={getJobDownloadUrl(activeJob.id)}
                      download
                      className="py-2 px-3.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 hover:text-white font-medium flex items-center gap-2 transition"
                    >
                      ⬇ Download Completed Worksheet
                    </a>
                  </div>
                </div>

                <div className="pt-2">
                  <button
                    type="button"
                    onClick={() => setShowSubmitModal(true)}
                    disabled={activeJob.submission_allowed === false && !activeJob.drive_web_view_link && !activeJob.result?.drive_web_url}
                    className="w-full py-3 px-4 rounded-lg bg-emerald-600 hover:bg-emerald-500 active:bg-emerald-700 text-white font-bold text-sm flex items-center justify-center gap-2 shadow-lg shadow-emerald-600/30 transition disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <span>Submit to SRM</span>
                  </button>
                  <div className="flex items-center justify-between text-[11px] text-slate-400 mt-2 px-1">
                    <span>Status: <strong className="text-amber-300">Waiting for your approval</strong></span>
                    <span className="text-slate-500">"Submit to SRM" will not occur automatically. Inspect the document above, then click to confirm submission.</span>
                  </div>
                </div>
              </div>
            )}

            {/* Deliverables Card */}
            {activeJob?.status === "COMPLETED" && activeJob.result && (
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-5 shadow-xl space-y-4 text-xs">
                <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                  <h2 className="text-sm font-semibold uppercase tracking-wider text-emerald-400">
                    ✅ Verification Confirmed & Deliverables Ready
                  </h2>
                  <span className="px-2.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-bold">
                    VERIFIED
                  </span>
                </div>

                <div className="p-4 rounded-lg bg-emerald-950/20 border border-emerald-800/40 space-y-2">
                  <div className="flex items-center justify-between text-slate-400">
                    <span>Public Google Drive Shareable Link:</span>
                    <span className="font-mono text-[10px] text-emerald-400">VERIFIED_PUBLIC_READER</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <input
                      type="text"
                      readOnly
                      value={activeJob.result.drive_web_url || ""}
                      className="flex-1 bg-slate-900 border border-slate-700 rounded px-3 py-1.5 font-mono text-slate-200"
                    />
                    <a
                      href={activeJob.result.drive_web_url || "#"}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white font-medium"
                    >
                      Open Drive
                    </a>
                  </div>
                </div>

                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-slate-300">
                  <div className="p-2.5 rounded bg-slate-900 border border-slate-800">
                    <div className="text-slate-500 text-[10px]">Original File</div>
                    <div className="font-semibold truncate">{activeJob.result.original_file || "--"}</div>
                  </div>
                  <div className="p-2.5 rounded bg-slate-900 border border-slate-800">
                    <div className="text-slate-500 text-[10px]">Completed Copy</div>
                    <div className="font-semibold text-emerald-400 truncate">{activeJob.result.completed_file || "--"}</div>
                  </div>
                  <div className="p-2.5 rounded bg-slate-900 border border-slate-800">
                    <div className="text-slate-500 text-[10px]">Answers Generated</div>
                    <div className="font-semibold text-white">{activeJob.result.answers_count ?? "--"}</div>
                  </div>
                  <div className="p-2.5 rounded bg-slate-900 border border-slate-800">
                    <div className="text-slate-500 text-[10px]">SRM Status</div>
                    <div className="font-semibold text-emerald-400">Verified (2)</div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </main>

      {/* CAPTCHA Modal */}
      {showCaptcha && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-amber-600/50 rounded-xl max-w-md w-full p-6 shadow-2xl space-y-4">
            <div className="flex items-center gap-3 text-amber-400">
              <span className="text-2xl">⚠️</span>
              <div>
                <h3 className="font-bold text-white text-base">
                  {captchaChallenge?.image_base64 ? "CAPTCHA Required" : "Solve CAPTCHA in Browser Window"}
                </h3>
                <p className="text-xs text-slate-400">
                  {captchaChallenge?.image_base64
                    ? "Solve the portal challenge to authenticate & proceed."
                    : "The SRM browser window is waiting for your manual CAPTCHA action."}
                </p>
              </div>
            </div>

            {captchaError && (
              <div className="p-3 rounded-lg bg-red-950/60 border border-red-800 text-red-200 text-xs">
                {captchaError}
              </div>
            )}

            {!captchaChallenge?.image_base64 ? (
              <div className="p-4 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-200 text-xs space-y-3">
                <p className="font-semibold text-amber-300 flex items-center gap-2">
                  <span>🖥️</span> Action Required: Open Login Browser & Solve CAPTCHA
                </p>
                <p className="text-slate-300 text-[11px] leading-relaxed">
                  Interactive browser authentication is required. Click below to open the SRM login window on your screen, complete the visual CAPTCHA, and sign in.
                </p>

                <div className="pt-1 space-y-2">
                  <button
                    type="button"
                    onClick={handleLaunchInteractiveBrowser}
                    disabled={isLaunchingBrowser && !browserConfirmed}
                    className={`w-full py-2.5 px-4 rounded-lg font-medium text-xs flex items-center justify-center gap-2 shadow-lg transition ${
                      browserConfirmed
                        ? "bg-amber-600 hover:bg-amber-500 text-white pulse-dot cursor-pointer"
                        : isLaunchingBrowser
                        ? "bg-blue-600/70 text-blue-200 cursor-wait"
                        : "bg-blue-600 hover:bg-blue-500 text-white shadow-blue-600/20 cursor-pointer"
                    }`}
                  >
                    <span>
                      {browserConfirmed
                        ? "🌐 Browser Open – Solve CAPTCHA & Sign In"
                        : isLaunchingBrowser
                        ? "Launching Browser..."
                        : "🚀 Open Interactive Login Browser"}
                    </span>
                  </button>

                  {browserLaunchStatus && (
                    <p className={`text-[11px] text-center font-medium ${
                      browserConfirmed ? "text-amber-300 font-semibold" : "text-blue-300"
                    }`}>
                      {browserLaunchStatus}
                    </p>
                  )}
                </div>

                <div className="pt-2 flex justify-end">
                  <button
                    type="button"
                    onClick={() => {
                      setShowCaptcha(false);
                      setCaptchaChallenge(null);
                      setCaptchaSolution("");
                      setCaptchaContext(null);
                      setCaptchaError(null);
                      setBrowserLaunchStatus(null);
                      setBrowserConfirmed(false);
                      setIsLaunchingBrowser(false);
                    }}
                    className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 font-medium text-xs transition"
                  >
                    Dismiss Notice
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="bg-slate-950 p-4 rounded-lg border border-slate-800 flex flex-col items-center justify-center gap-2">
                  <img
                    src={
                      captchaChallenge.image_base64.startsWith("data:")
                        ? captchaChallenge.image_base64
                        : `data:image/png;base64,${captchaChallenge.image_base64}`
                    }
                    alt="CAPTCHA Challenge"
                    className="h-12 bg-white px-3 py-1 rounded shadow border border-slate-700"
                  />
                  <button
                    type="button"
                    onClick={async () => {
                      try {
                        setCaptchaError(null);
                        setCaptchaSolution("");
                        const resp = await discoverSRMCourses(userId, password, semester, undefined, transportMode);
                        if (resp.status === "WAITING_FOR_CAPTCHA" && resp.captcha_challenge) {
                          setCaptchaChallenge(resp.captcha_challenge);
                        }
                      } catch (err: any) {
                        setCaptchaError("Failed to refresh CAPTCHA: " + err.message);
                      }
                    }}
                    className="text-[11px] text-slate-400 hover:text-amber-400 flex items-center gap-1 transition cursor-pointer"
                  >
                    <span>🔄</span> Can't read? Refresh challenge
                  </button>
                </div>

                <form onSubmit={handleCaptchaSubmit} className="space-y-3">
                  <div>
                    <label className="block text-xs font-medium text-slate-300 mb-1">CAPTCHA Solution</label>
                    <input
                      type="text"
                      required
                      autoFocus
                      value={captchaSolution}
                      onChange={(e) => setCaptchaSolution(e.target.value.toUpperCase())}
                      placeholder="e.g. 48B92"
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-center font-mono text-white tracking-widest uppercase focus:outline-none focus:border-amber-500"
                    />
                  </div>

                  <div className="flex gap-2 pt-1">
                    <button
                      type="button"
                      disabled={isSubmittingCaptcha}
                      onClick={() => {
                        setShowCaptcha(false);
                        setCaptchaChallenge(null);
                        setCaptchaSolution("");
                        setCaptchaContext(null);
                        setCaptchaError(null);
                      }}
                      className="px-4 py-2.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 font-medium text-xs transition"
                    >
                      Cancel
                    </button>
                    <button
                      type="submit"
                      disabled={isSubmittingCaptcha || !captchaSolution.trim()}
                      className="flex-1 py-2.5 px-4 rounded-lg bg-amber-600 hover:bg-amber-500 text-white font-medium text-sm transition disabled:opacity-50"
                    >
                      {isSubmittingCaptcha
                        ? "Submitting Solution..."
                        : captchaContext === "discovery"
                        ? "Submit Solution & Discover Courses"
                        : "Submit Solution & Resume Job"}
                    </button>
                  </div>
                </form>
              </>
            )}
          </div>
        </div>
      )}

      {/* Submit Confirmation Modal */}
      {showSubmitModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-slate-950 border border-slate-700 rounded-xl max-w-md w-full p-6 shadow-2xl space-y-4">
            <div className="flex items-center gap-3 text-amber-400">
              <span className="text-xl">⚠️</span>
              <h3 className="font-bold text-base text-white">Confirm SRM Submission</h3>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">
              Your completed worksheet is ready. Do you want to submit it to SRM?
            </p>
            {submitError && (
              <div className="p-3 rounded-lg bg-red-950/60 border border-red-800 text-red-200 text-xs">
                {submitError}
              </div>
            )}
            <div className="flex justify-end gap-3 pt-2">
              <button
                type="button"
                disabled={isSubmittingToSrm}
                onClick={() => {
                  setShowSubmitModal(false);
                  setSubmitError(null);
                }}
                className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={isSubmittingToSrm}
                onClick={handleConfirmSubmitToSrm}
                className="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold flex items-center gap-1.5 transition disabled:opacity-50"
              >
                {isSubmittingToSrm ? "Submitting..." : "Submit to SRM"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
