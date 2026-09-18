"use client";

import React, { useState, useEffect } from "react";
import {
  checkDriveStatus,
  getDriveAuthUrl,
  discoverSRMCourses,
  createJob,
  getJob,
  submitCaptchaSolution,
  CourseItem,
  WorksheetItem,
  JobResponse,
} from "../lib/api";

const PIPELINE_STEPS = [
  { key: "PENDING", title: "1. PENDING", desc: "Job received & enqueued in broker" },
  { key: "RUNNING", title: "2. RUNNING", desc: "Worker initialized, connecting to SRM" },
  { key: "WAITING_FOR_CAPTCHA", title: "3. WAITING_FOR_CAPTCHA", desc: "Paused if portal presents CAPTCHA" },
  { key: "DOWNLOADING", title: "4. DOWNLOADING", desc: "Downloading worksheet document" },
  { key: "PROCESSING", title: "5. PROCESSING", desc: "Parsing & answering via FreeLLMAPI" },
  { key: "UPLOADING", title: "6. UPLOADING", desc: "Storing in Google Drive (Anyone with link / Viewer)" },
  { key: "SUBMITTING", title: "7. SUBMITTING", desc: "Submitting verified link to SRM portal" },
  { key: "VERIFYING", title: "8. VERIFYING", desc: "Confirming portal practice status update" },
  { key: "COMPLETED", title: "9. COMPLETED", desc: "Successfully finished and verified" },
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

  // CAPTCHA Modal State
  const [showCaptcha, setShowCaptcha] = useState(false);
  const [captchaChallenge, setCaptchaChallenge] = useState<any>(null);
  const [captchaSolution, setCaptchaSolution] = useState("");
  const [isSubmittingCaptcha, setIsSubmittingCaptcha] = useState(false);

  // Load Google Drive Status on Mount
  useEffect(() => {
    checkDrive();
  }, []);

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
    try {
      const resp = await discoverSRMCourses(userId, password, semester, undefined, transportMode);
      if (resp.status === "WAITING_FOR_CAPTCHA") {
        setCaptchaChallenge(resp.captcha_challenge);
        setShowCaptcha(true);
        return;
      }
      setCourses(resp.courses);
      if (resp.courses.length > 0) {
        setSelectedCourseCode(resp.courses[0].course_code);
      }
    } catch (err: any) {
      setDiscoveryError(err.message);
    } finally {
      setIsDiscovering(false);
    }
  };

  // Start Automation Job
  const handleStartJob = async () => {
    if (!selectedCourseCode || !selectedWorksheet) return;
    setIsStartingJob(true);
    setJobError(null);
    try {
      const wsId = selectedWorksheet.worksheet_id || `${selectedWorksheet.session}${selectedWorksheet.slo}`;
      const job = await createJob({
        user_id: userId,
        course_id: selectedCourseCode,
        semester_id: String(semester),
        worksheet_id: wsId,
        session: selectedWorksheet.session,
        slo: selectedWorksheet.slo,
        transport_mode: transportMode,
        credentials: { USER_ID: userId, PASSWORD: password },
      });
      setActiveJob(job);
    } catch (err: any) {
      if (err.status === 409 && err.existingJobId) {
        alert("Notice: " + err.message);
        // Resume tracking existing active job
        const existing = await getJob(err.existingJobId);
        setActiveJob(existing);
      } else {
        setJobError(err.message);
      }
    } finally {
      setIsStartingJob(false);
    }
  };

  // Poll Active Job
  useEffect(() => {
    if (!activeJob || activeJob.status === "COMPLETED" || activeJob.status === "FAILED") {
      return;
    }

    const timer = setInterval(async () => {
      try {
        const updated = await getJob(activeJob.id);
        setActiveJob(updated);

        if (updated.status === "WAITING_FOR_CAPTCHA") {
          setCaptchaChallenge(updated.captcha_challenge);
          setShowCaptcha(true);
        } else {
          setShowCaptcha(false);
        }
      } catch (e) {
        console.warn("Poll failed:", e);
      }
    }, 1500);

    return () => clearInterval(timer);
  }, [activeJob]);

  // Submit CAPTCHA
  const handleCaptchaSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeJob || !captchaSolution) return;
    setIsSubmittingCaptcha(true);
    try {
      const updated = await submitCaptchaSolution(activeJob.id, captchaSolution);
      setActiveJob(updated);
      setShowCaptcha(false);
      setCaptchaSolution("");
    } catch (err: any) {
      alert("Failed to submit CAPTCHA: " + err.message);
    } finally {
      setIsSubmittingCaptcha(false);
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
                    }}
                    className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-emerald-500"
                  >
                    {courses.map((c) => (
                      <option key={c.course_code} value={c.course_code}>
                        {c.course_code} - {c.course_name} ({c.worksheets.length} worksheets)
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-slate-300 font-medium mb-1">Available Worksheets</label>
                  <div className="space-y-2 max-h-48 overflow-y-auto pr-1">
                    {currentCourse && currentCourse.worksheets.length > 0 ? (
                      currentCourse.worksheets.map((ws) => {
                        const isSelected =
                          selectedWorksheet &&
                          selectedWorksheet.session === ws.session &&
                          selectedWorksheet.slo === ws.slo;
                        return (
                          <div
                            key={ws.worksheet_id || `${ws.session}-${ws.slo}`}
                            onClick={() => setSelectedWorksheet(ws)}
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
                                onChange={() => setSelectedWorksheet(ws)}
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
                      })
                    ) : (
                      <p className="text-slate-500 italic">No worksheets available for this course.</p>
                    )}
                  </div>
                </div>

                <button
                  type="button"
                  onClick={handleStartJob}
                  disabled={!selectedWorksheet || isStartingJob}
                  className="w-full py-2.5 px-4 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium text-sm transition disabled:opacity-50"
                >
                  {isStartingJob ? "Starting Background Task..." : "Start Background Automation"}
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
                    activeJob?.status === "COMPLETED"
                      ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                      : activeJob?.status === "FAILED"
                      ? "bg-red-500/10 text-red-400 border border-red-500/20"
                      : activeJob?.status === "WAITING_FOR_CAPTCHA"
                      ? "bg-amber-500/10 text-amber-400 border border-amber-500/20 pulse-dot"
                      : "bg-blue-500/10 text-blue-400 border border-blue-500/20"
                  }`}
                >
                  {activeJob ? activeJob.status : "IDLE"}
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
                    {activeJob ? activeJob.current_step || activeJob.status : "Waiting to start"}
                  </span>
                </div>
              </div>

              {/* 10-State Stepper */}
              <div className="relative border-l-2 border-slate-800 ml-4 pl-6 space-y-4 py-1">
                {PIPELINE_STEPS.map((step, idx) => {
                  const currentIdx = activeJob
                    ? PIPELINE_STEPS.findIndex((s) => s.key === activeJob.status)
                    : -1;
                  const isCompleted = activeJob?.status === "COMPLETED" || idx < currentIdx;
                  const isActive = activeJob?.status === step.key;

                  let dotClass = "bg-slate-800 border-slate-700";
                  let textClass = "text-slate-500";

                  if (activeJob?.status === "FAILED") {
                    if (idx <= currentIdx) {
                      dotClass = "bg-red-600 border-red-400";
                      textClass = "text-red-400 font-semibold";
                    }
                  } else if (isCompleted) {
                    dotClass = "bg-emerald-500 border-emerald-400";
                    textClass = "text-emerald-400 font-semibold";
                  } else if (isActive) {
                    dotClass = "bg-blue-500 border-white pulse-dot";
                    textClass = "text-blue-300 font-semibold";
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
                <h3 className="font-bold text-white text-base">CAPTCHA Required</h3>
                <p className="text-xs text-slate-400">Solve the portal challenge to resume background processing.</p>
              </div>
            </div>

            {captchaChallenge?.image_base64 && (
              <div className="bg-slate-950 p-4 rounded-lg border border-slate-800 flex justify-center">
                <img
                  src={
                    captchaChallenge.image_base64.startsWith("data:")
                      ? captchaChallenge.image_base64
                      : `data:image/png;base64,${captchaChallenge.image_base64}`
                  }
                  alt="CAPTCHA"
                  className="h-12 bg-white px-2 rounded"
                />
              </div>
            )}

            <form onSubmit={handleCaptchaSubmit} className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-slate-300 mb-1">CAPTCHA Solution</label>
                <input
                  type="text"
                  required
                  value={captchaSolution}
                  onChange={(e) => setCaptchaSolution(e.target.value.toUpperCase())}
                  placeholder="e.g. 48B92"
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-center font-mono text-white tracking-widest uppercase focus:outline-none focus:border-amber-500"
                />
              </div>

              <button
                type="submit"
                disabled={isSubmittingCaptcha}
                className="w-full py-2.5 px-4 rounded-lg bg-amber-600 hover:bg-amber-500 text-white font-medium text-sm transition"
              >
                {isSubmittingCaptcha ? "Submitting Solution..." : "Submit Solution & Resume Job"}
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
