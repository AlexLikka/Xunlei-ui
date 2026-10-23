'use strict';
const $ = s => document.querySelector(s);
let registering = false;
let submitting = false;
function setMode(register) {
  if (submitting) return;
  registering = register;
  $('#title').textContent = register ? '创建你的工作空间' : '登录你的工作空间';
  $('#hint').textContent = register ? '注册后拥有独立空间，可以添加自己的机器。密码至少 12 位。' : '管理自己的下载机器，查看任务与运行状态。';
  $('#submit').textContent = register ? '注册并进入' : '登录';
  $('#password').minLength = register ? 12 : 1;
  $('#password').autocomplete = register ? 'new-password' : 'current-password';
  $('#password').value = $('#confirm-password').value = '';
  $('#confirm-label').hidden = !register;
  $('#confirm-password').required = register;
  $('#mode-login').setAttribute('aria-pressed', String(!register));
  $('#mode-register').setAttribute('aria-pressed', String(register));
  $('#message').textContent = '';
}
async function send(action, body) {
  const response = await fetch('/api/auth/' + action, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '请求失败，请稍后重试');
}
$('#mode-login').onclick = () => setMode(false);
$('#mode-register').onclick = () => setMode(true);
$('#login-form').onsubmit = async event => {
  event.preventDefault();
  if (submitting) return;
  if (registering && $('#password').value !== $('#confirm-password').value) {
    $('#message').textContent = '两次输入的密码不一致'; return;
  }
  submitting = true;
  for (const id of ['submit', 'mode-login', 'mode-register']) $('#' + id).disabled = true;
  $('#message').textContent = registering ? '正在创建工作空间…' : '正在登录…';
  const body = {username: $('#username').value.trim(), password: $('#password').value};
  try {
    if (registering) await send('register', body);
    await send('login', body);
    location.replace('/');
  } catch (error) {
    $('#message').textContent = error.message;
  } finally {
    submitting = false;
    for (const id of ['submit', 'mode-login', 'mode-register']) $('#' + id).disabled = false;
  }
};
fetch('/api/auth/status').then(async response => {
  if (!response.ok) throw Error('服务不可用');
  const state = await response.json();
  if (state.authenticated) location.replace('/');
}).catch(error => { $('#message').textContent = error.message; });
