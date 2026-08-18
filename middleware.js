// Password gate for the whole app (pages + /api/*).
// Runs on Vercel's Edge Runtime, so it works alongside the existing
// Python serverless functions without touching any of them.
//
// Requires one environment variable in Vercel:
//   SITE_PASSWORD - the password visitors must enter
//
// Optional:
//   AUTH_SECRET - random string used to sign the auth cookie.
//                 If not set, SITE_PASSWORD is used as the signing key
//                 (fine for this use case, but setting AUTH_SECRET
//                 separately is slightly better practice).

export const config = {
  matcher: '/((?!_next/static).*)',
};

const COOKIE_NAME = 'dhg_auth';
const MAX_AGE_SECONDS = 60 * 60 * 24 * 30; // 30 days
const LOGIN_PATH = '/__login';

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(message));
  return toHex(sig);
}

async function makeCookieValue(secret) {
  const expiry = Date.now() + MAX_AGE_SECONDS * 1000;
  const sig = await hmac(secret, String(expiry));
  return `${expiry}.${sig}`;
}

async function isValidCookie(value, secret) {
  if (!value) return false;
  const [expiryStr, sig] = value.split('.');
  if (!expiryStr || !sig) return false;
  const expiry = Number(expiryStr);
  if (!Number.isFinite(expiry) || expiry < Date.now()) return false;
  const expectedSig = await hmac(secret, expiryStr);
  return expectedSig === sig;
}

function loginPageHtml(error, next) {
  const safeNext = next && next.startsWith('/') ? next : '/';
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign in — Detective Hawk Games</title>
<style>
  body { font-family: system-ui, sans-serif; background: #241221; color: #f5ecf0;
         display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
  form { background: #f5efe6; color: #241221; padding: 2rem 2.5rem; border-radius: 10px;
         box-shadow: 0 10px 30px rgba(0,0,0,0.35); width: 280px; }
  h1 { font-size: 1.1rem; margin: 0 0 1.25rem; }
  input[type="password"] { width: 100%; padding: 0.6rem 0.7rem; font-size: 1rem;
         border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; margin-bottom: 0.9rem; }
  button { width: 100%; padding: 0.6rem; font-size: 1rem; border: none; border-radius: 6px;
         background: #7a3a63; color: white; cursor: pointer; }
  button:hover { background: #632e50; }
  .error { color: #b3261e; font-size: 0.85rem; margin: -0.5rem 0 0.9rem; }
</style>
</head>
<body>
  <form method="POST" action="${LOGIN_PATH}">
    <h1>Detective Hawk Games — Internal Tool</h1>
    ${error ? '<div class="error">Wrong password. Try again.</div>' : ''}
    <input type="hidden" name="next" value="${safeNext}">
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Enter</button>
  </form>
</body>
</html>`;
}

export default async function middleware(request) {
  const url = new URL(request.url);
  const secret = process.env.AUTH_SECRET || process.env.SITE_PASSWORD;
  const sitePassword = process.env.SITE_PASSWORD;

  if (!sitePassword) {
    // Fail safe: if the password isn't configured, don't lock everyone
    // out silently — but don't leave the app open either. Show an
    // explicit error instead of pretending nothing's wrong.
    return new Response(
      'Site password not configured. Set SITE_PASSWORD in Vercel project settings.',
      { status: 500 }
    );
  }

  // Handle login form submission
  if (url.pathname === LOGIN_PATH && request.method === 'POST') {
    const form = await request.formData();
    const password = form.get('password');
    const next = form.get('next') || '/';

    if (password === sitePassword) {
      const cookieValue = await makeCookieValue(secret);
      const headers = new Headers();
      headers.set('Location', new URL(next, url.origin).toString());
      headers.append(
        'Set-Cookie',
        `${COOKIE_NAME}=${cookieValue}; Path=/; Max-Age=${MAX_AGE_SECONDS}; HttpOnly; Secure; SameSite=Lax`
      );
      return new Response(null, { status: 302, headers });
    }

    return new Response(loginPageHtml(true, next), {
      status: 401,
      headers: { 'Content-Type': 'text/html' },
    });
  }

  // Serve the login page itself
  if (url.pathname === LOGIN_PATH) {
    return new Response(loginPageHtml(false, url.searchParams.get('next')), {
      status: 200,
      headers: { 'Content-Type': 'text/html' },
    });
  }

  // Everything else: check the cookie
  const cookieHeader = request.headers.get('cookie') || '';
  const match = cookieHeader.match(new RegExp(`${COOKIE_NAME}=([^;]+)`));
  const cookieValue = match ? match[1] : null;

  if (await isValidCookie(cookieValue, secret)) {
    return; // authenticated, let it through
  }

  const next = encodeURIComponent(url.pathname + url.search);
  return Response.redirect(new URL(`${LOGIN_PATH}?next=${next}`, url.origin), 302);
}
