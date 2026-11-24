'use strict';
const form = document.getElementById('login-form');
const err = document.getElementById('login-error');
const btn = document.getElementById('login-submit');
const pw = document.getElementById('password');
document.getElementById('pw-toggle').addEventListener('click', (e) => {
  const show = pw.type === 'password';
  pw.type = show ? 'text' : 'password';
  e.currentTarget.textContent = show ? 'Hide' : 'Show';
});
document.getElementById('username').focus();
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  err.classList.add('hidden');
  const username = form.username.value.trim();
  const password = form.password.value;
  if (!username || !password) { err.textContent = 'Enter your username and password.'; err.classList.remove('hidden'); return; }
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const res = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || 'Could not sign in.');
    window.location.replace('/');
  } catch (ex) {
    err.textContent = ex.message === 'Failed to fetch' ? 'Cannot reach the server. Check your connection.' : ex.message;
    err.classList.remove('hidden');
    btn.disabled = false; btn.textContent = 'Sign in';
    pw.select();
  }
});
