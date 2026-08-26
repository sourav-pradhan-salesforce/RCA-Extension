function toggleEdit(btn) {
  const body = document.getElementById('rcaBody');
  const on   = body.contentEditable !== 'true';
  body.contentEditable = on ? 'true' : 'false';
  body.classList.toggle('edit-mode', on);
  btn.textContent = on ? '✓ Done Editing' : '✏ Edit';
}
