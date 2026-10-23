# Rebo Live Sign-In

Open the plugin page `astrbot_plugin_rebo_live/settings` and select a sign-in method.

| Method | Steps |
| --- | --- |
| Password | Existing sign-in and re-login controls are under the password option. |
| SMS | Enter the phone number → request a code → complete the platform's human verification → enter the six-digit SMS code → sign in. |

Requesting a code opens a controlled verification dialog. Complete the official challenge manually before an SMS is requested. The plugin keeps its opaque sandbox; only the fixed verification document receives cookie access. Phone numbers, passwords and WebUI tokens are not passed to that document.

SMS requests have a 60-second minimum interval and are never retried automatically after a timeout. Cancellation, an unavailable SDK or failed human verification does not send an SMS. Failures distinguish an unreachable script, loading timeout and unavailable service. Refresh the entire WebUI after this update to refresh the host window as well. Human verification is not bypassed. Enter the code on the page, not in chat.

Verification codes are not written to files or logs. Successful SMS sign-in retains session tokens and clears the previously saved password. Paused rooms stay paused; message text and intervals are unchanged. An expired session requires manual sign-in, not automatic SMS requests.

Inputs and sign-in options become available when the page reports ready. If initialization does not finish, refresh the entire WebUI to obtain fresh page authorization; do not restart the plugin or sign in to Rebo again. Scripts support sandboxed loading behind Cloudflare Access without removing access checks or relaxing the sandbox. Authorization and script failures remain visible and do not trigger automatic retries.

![SMS sign-in page](../../zh/platform/images/rebo-sms-login.png)

![Controlled official verification dialog requiring manual completion](../../zh/platform/images/rebo-human-verification-mobile.png)
