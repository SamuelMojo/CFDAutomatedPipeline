// Sign-in and access requests for the CFD pipeline site. Talks to Amazon Cognito directly over HTTPS.
// The signed-in screen (uploads, the run list) lives in jobs.js; this file tells it when someone signs in or out
// (the "cfd:signedin" and "cfd:signedout" events) and hands it a fresh token through window.CFD_AUTH.
// No inline scripts and no innerHTML: the page's Content-Security-Policy forbids them.
(() => {
  "use strict";

  // Region and app client ID, written to config.js by Terraform (public identifiers, not secrets).
  const cfg = window.CFD_CONFIG;
  const ENDPOINT = `https://cognito-idp.${cfg.region}.amazonaws.com/`;
  // The screens in index.html; exactly one is visible at a time (see show()).
  const VIEWS = ["signin", "newpw", "mfa", "forgot", "reset", "access", "app"];
  // sessionStorage key for the tokens. sessionStorage is cleared when the tab closes.
  const KEY = "cfd.tokens";

  const $ = (id) => document.getElementById(id);

  // What the next step of a sign-in needs to remember.
  let pending = { email: "", username: "", session: "" };

  // ---- small helpers ------------------------------------------------------

  // Keep the tokens for this tab only. Storage can be blocked (private windows), so every access is guarded.
  function store(tokens) {
    try { sessionStorage.setItem(KEY, JSON.stringify(tokens)); } catch (_) { /* private mode */ }
  }
  function load() {
    try { return JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch (_) { return null; }
  }
  function forget() {
    try { sessionStorage.removeItem(KEY); } catch (_) { /* ignore */ }
  }

  // Read the payload of a token to show the email and check the expiry time. The signature is NOT checked here;
  // that is the server's job, and nothing in this page trusts the result for security.
  function decode(jwt) {
    try {
      const part = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      return JSON.parse(atob(part + "=".repeat((4 - (part.length % 4)) % 4)));
    } catch (_) {
      return {};
    }
  }

  // Show a status line above the form: kind is "error" (default) or "ok"; empty text hides it.
  function message(text, kind) {
    const el = $("message");
    el.textContent = text || "";
    el.className = "message " + (kind || "error");
    el.hidden = !text;
  }

  // The password fields of a form: the sign-in password, or the pair of new-password fields.
  function passwordFields(form) {
    return form.querySelectorAll('input[autocomplete="current-password"], input[autocomplete="new-password"]');
  }

  // Each "Show password" tick box reveals or hides every password field in its own form.
  document.querySelectorAll(".show-password").forEach((box) => {
    box.addEventListener("change", () => {
      passwordFields(box.form).forEach((field) => { field.type = box.checked ? "text" : "password"; });
    });
  });

  // Hide every password again, so none stays visible after the screen changes.
  function hidePasswords() {
    document.querySelectorAll(".show-password").forEach((box) => {
      box.checked = false;
      passwordFields(box.form).forEach((field) => { field.type = "password"; });
    });
  }

  // Switch to one screen, hide any shown passwords, clear any message and put the cursor in its first field.
  // The signed-in screen is wider than the sign-in forms, and nothing there should grab the keyboard focus.
  function show(name) {
    VIEWS.forEach((v) => { $("view-" + v).hidden = v !== name; });
    $("card").classList.toggle("wide", name === "app");
    hidePasswords();
    message("");
    const first = name === "app" ? null : $("view-" + name).querySelector("input");
    if (first) first.focus();
  }

  // Lock or unlock a form while a request is in flight, so a double click cannot send it twice.
  function busy(form, on) {
    form.querySelectorAll("button, input").forEach((el) => { el.disabled = on; });
  }

  // Returns a sentence describing what is wrong with a new password, or "" if it is acceptable.
  // The rules repeat the Cognito policy in auth.tf so people see the problem before sending; Cognito still enforces it.
  function passwordProblem(a, b) {
    if (a !== b) return "The two passwords do not match.";
    if (a.length < 12) return "Use at least 12 characters.";
    if (!/[a-z]/.test(a) || !/[A-Z]/.test(a) || !/[0-9]/.test(a) || !/[^A-Za-z0-9]/.test(a)) {
      return "Include an upper-case letter, a lower-case letter, a number and a symbol.";
    }
    return "";
  }

  // Plain-language messages for Cognito error codes. Wrong email and wrong password deliberately read the same.
  const FRIENDLY = {
    NotAuthorizedException: "Incorrect email or password.",
    UserNotFoundException: "Incorrect email or password.",
    UserNotConfirmedException: "This account is not ready yet. Contact the site owner.",
    CodeMismatchException: "That code is not right. Check it and try again.",
    ExpiredCodeException: "That code has expired. Request a new one.",
    InvalidPasswordException: "That password does not meet the rules.",
    LimitExceededException: "Too many attempts. Wait a few minutes and try again.",
    TooManyRequestsException: "Too many attempts. Wait a few minutes and try again.",
    PasswordResetRequiredException: "Reset your password with the link below.",
    InvalidParameterException: "Something about that request was not valid.",
  };

  // Call one Cognito API action (for example "InitiateAuth") and return the parsed JSON.
  // On an error the thrown Error has .code set to Cognito's short error name.
  async function cognito(target, body) {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityProviderService." + target,
      },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.message || "Request failed");
      err.code = String(data.__type || "").split("#").pop();
      throw err;
    }
    return data;
  }

  // The message to show for an error: one already worded for the person (err.shown), a network failure
  // (TypeError from fetch) or a known Cognito code.
  function friendly(err) {
    if (err.shown) return err.message;
    if (err instanceof TypeError) return "Could not reach the sign-in service. Check your connection.";
    return FRIENDLY[err.code] || "Something went wrong. Please try again.";
  }

  // Runs a form handler with the form locked and errors shown.
  function on(formId, handler) {
    const form = $(formId);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      busy(form, true);
      try {
        await handler();
      } catch (err) {
        message(friendly(err), "error");
      } finally {
        busy(form, false);
      }
    });
  }

  // ---- sign-in steps ------------------------------------------------------

  // Save the tokens from a sign-in or a refresh and return them.
  function saveTokens(result) {
    const prev = load() || {};
    const tokens = {
      access: result.AccessToken,
      id: result.IdToken,
      // A refresh does not return a new refresh token, so keep the old one.
      refresh: result.RefreshToken || prev.refresh,
    };
    store(tokens);
    return tokens;
  }

  // Save the tokens and show the signed-in screen with the account's email; jobs.js starts loading the runs.
  function enterApp(result) {
    const tokens = saveTokens(result);
    $("app-email").textContent = decode(tokens.id).email || "your account";
    show("app");
    document.dispatchEvent(new Event("cfd:signedin"));
  }

  // The tab is no longer signed in (sign-out, or tokens that can no longer be renewed): clear them, tell
  // jobs.js to drop what it shows, and go back to the sign-in screen.
  function leave(why) {
    forget();
    $("app-email").textContent = "";
    document.dispatchEvent(new Event("cfd:signedout"));
    show("signin");
    if (why) message(why, "error");
  }

  // The ID token for calls to the site's API, renewed first if it has under a minute left. One renewal at a time.
  let renewing = null;
  async function idToken() {
    const tokens = load();
    if (!tokens || !tokens.id) throw sessionEnded();
    if ((decode(tokens.id).exp || 0) * 1000 > Date.now() + 60000) return tokens.id;
    if (!renewing) {
      renewing = (async () => {
        try {
          if (!tokens.refresh) throw sessionEnded();
          const r = await cognito("InitiateAuth", {
            AuthFlow: "REFRESH_TOKEN_AUTH",
            ClientId: cfg.clientId,
            AuthParameters: { REFRESH_TOKEN: tokens.refresh },
          });
          return saveTokens(r.AuthenticationResult).id;
        } catch (err) {
          if (err.code === "NotAuthorizedException" || err.shown) throw sessionEnded();
          throw err;                                                   // a network problem: try again later
        } finally {
          renewing = null;
        }
      })();
    }
    return renewing;
  }

  // The error for "you must sign in again"; leaving the screen happens here so every caller behaves the same.
  function sessionEnded() {
    const err = new Error("Your session has ended. Please sign in again.");
    err.shown = true;
    err.sessionEnded = true;
    if (load()) leave(err.message);
    return err;
  }

  window.CFD_AUTH = { idToken, sessionEnded };

  // Deal with Cognito's answer to a sign-in step: tokens mean success; otherwise show the screen for the next
  // challenge (choose a new password, or enter an authenticator code).
  function handleAuth(r) {
    if (r.AuthenticationResult) {
      enterApp(r.AuthenticationResult);
      return;
    }
    const params = r.ChallengeParameters || {};
    pending.username = params.USER_ID_FOR_SRP || pending.email;
    pending.session = r.Session;
    if (r.ChallengeName === "NEW_PASSWORD_REQUIRED") {
      show("newpw");
    } else if (r.ChallengeName === "SOFTWARE_TOKEN_MFA") {
      show("mfa");
    } else {
      message("This account needs a sign-in step this page does not support yet.", "error");
    }
  }

  // Step 1: email and password. The password field is cleared as soon as it has been sent.
  on("form-signin", async () => {
    pending.email = $("signin-email").value.trim().toLowerCase();
    const r = await cognito("InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: cfg.clientId,
      AuthParameters: { USERNAME: pending.email, PASSWORD: $("signin-password").value },
    });
    $("signin-password").value = "";
    handleAuth(r);
  });

  // Step 2 (first sign-in only): replace the emailed temporary password with the person's own.
  on("form-newpw", async () => {
    const a = $("newpw-1").value;
    const problem = passwordProblem(a, $("newpw-2").value);
    if (problem) { message(problem, "error"); return; }
    const r = await cognito("RespondToAuthChallenge", {
      ClientId: cfg.clientId,
      ChallengeName: "NEW_PASSWORD_REQUIRED",
      Session: pending.session,
      ChallengeResponses: { USERNAME: pending.username, NEW_PASSWORD: a },
    });
    $("newpw-1").value = "";
    $("newpw-2").value = "";
    handleAuth(r);
  });

  // Step 3 (only if an authenticator app is set up): the 6-digit code.
  on("form-mfa", async () => {
    const r = await cognito("RespondToAuthChallenge", {
      ClientId: cfg.clientId,
      ChallengeName: "SOFTWARE_TOKEN_MFA",
      Session: pending.session,
      ChallengeResponses: {
        USERNAME: pending.username,
        SOFTWARE_TOKEN_MFA_CODE: $("mfa-code").value.trim(),
      },
    });
    $("mfa-code").value = "";
    handleAuth(r);
  });

  // ---- forgotten password -------------------------------------------------

  on("form-forgot", async () => {
    pending.email = $("forgot-email").value.trim().toLowerCase();
    try {
      await cognito("ForgotPassword", { ClientId: cfg.clientId, Username: pending.email });
    } catch (err) {
      // Do not reveal whether the account exists; only surface throttling.
      if (err.code === "LimitExceededException" || err.code === "TooManyRequestsException" || err instanceof TypeError) throw err;
    }
    show("reset");
    message("If that account exists, a code has been emailed to it.", "ok");
  });

  // Second half of a password reset: the emailed code plus the new password.
  on("form-reset", async () => {
    const a = $("reset-1").value;
    const problem = passwordProblem(a, $("reset-2").value);
    if (problem) { message(problem, "error"); return; }
    await cognito("ConfirmForgotPassword", {
      ClientId: cfg.clientId,
      Username: pending.email,
      ConfirmationCode: $("reset-code").value.trim(),
      Password: a,
    });
    ["reset-code", "reset-1", "reset-2"].forEach((id) => { $(id).value = ""; });
    show("signin");
    message("Password changed. Sign in with the new one.", "ok");
  });

  // ---- request access -----------------------------------------------------------------------------------

  // Send the owner an access request. The site answers the same way whether or not the email is already known.
  on("form-access", async () => {
    const name = $("access-name").value.trim();
    const email = $("access-email").value.trim();
    if (name.length < 2) { message("Please enter your name.", "error"); return; }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { message("Please enter a valid email address.", "error"); return; }
    const res = await fetch(`${cfg.apiUrl}/request-access`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        email,
        organisation: $("access-org").value.trim(),
        message: $("access-message").value.trim(),
        website: $("access-extra").value,                         // the hidden field; only robots fill it in
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(res.status === 429 ? "Too many requests. Wait a minute and try again."
        : data.error || "The request could not be sent. Please try again.");
      err.shown = true;
      throw err;
    }
    ["access-name", "access-email", "access-org", "access-message", "access-extra"]
      .forEach((id) => { $(id).value = ""; });
    show("signin");
    message(data.message || "Thank you. If your request is approved you will get an email.", "ok");
  });
  $("go-access").addEventListener("click", () => {
    show("access");
    $("access-email").value = $("signin-email").value;
  });
  $("access-back").addEventListener("click", () => show("signin"));

  $("go-forgot").addEventListener("click", () => {
    show("forgot");
    $("forgot-email").value = $("signin-email").value;
  });
  $("forgot-back").addEventListener("click", () => show("signin"));

  // ---- sign out and returning visitors ------------------------------------

  $("signout").addEventListener("click", async () => {
    const tokens = load();
    leave("");
    if (tokens && tokens.access) {
      try { await cognito("GlobalSignOut", { AccessToken: tokens.access }); } catch (_) { /* already expired */ }
    }
  });

  // On page load: reuse the saved tokens if still valid, renew them with the refresh token if not,
  // otherwise show the sign-in screen.
  async function resume() {
    const tokens = load();
    if (!tokens) { show("signin"); return; }
    const exp = decode(tokens.access || "").exp || 0;
    if (exp * 1000 > Date.now() + 60000) {
      enterApp({ AccessToken: tokens.access, IdToken: tokens.id, RefreshToken: tokens.refresh });
      return;
    }
    if (tokens.refresh) {
      try {
        const r = await cognito("InitiateAuth", {
          AuthFlow: "REFRESH_TOKEN_AUTH",
          ClientId: cfg.clientId,
          AuthParameters: { REFRESH_TOKEN: tokens.refresh },
        });
        enterApp(r.AuthenticationResult);
        return;
      } catch (_) { /* fall through to sign-in */ }
    }
    leave("");
  }

  resume();
})();
