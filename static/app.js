// ブラウザ側の連打抑止は補助。サーバー側のトランザクション・一意制約が最終防衛線。
document.querySelectorAll('form[data-lock]').forEach(form => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = '処理しています…'; }
  });
});
window.addEventListener('pageshow', () => {
  document.querySelectorAll('button[disabled]').forEach(button => { button.disabled = false; button.textContent = button.dataset.label || '確定'; });
});
