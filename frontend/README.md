# SRM Automator Frontend

Next.js/React frontend interface for the SRM eCurricula worksheet automation platform.

## Key UI Roles:
1. **Job Dashboard**: Displays list of jobs and their real-time state:
   - `PENDING`
   - `RUNNING`
   - `WAITING_FOR_CAPTCHA`
   - `DOWNLOADING`
   - `PROCESSING`
   - `UPLOADING`
   - `SUBMITTING`
   - `VERIFYING`
   - `COMPLETED`
   - `FAILED`
2. **CAPTCHA Input Modal**: When a job enters `WAITING_FOR_CAPTCHA`, displays the rendered CAPTCHA image challenge and allows the user to solve and submit it back to the backend.
3. **Trigger New Job**: Allows users to input course/worksheet parameters and launch asynchronous background tasks without needing to keep the browser window open.
