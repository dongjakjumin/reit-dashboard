function foldTable(table) {
  const rows = Array.from(table.tBodies[0].rows);
  const buttonId = table.id + '-toggle';
  let button = document.getElementById(buttonId);
  if (!button) {
    button = document.createElement('button');
    button.id = buttonId;
    button.type = 'button';
    button.className = 'btn';
    button.setAttribute('aria-controls', table.id);
    table.closest('.tw').after(button);
  }
  function show(expanded) {
    rows.forEach((row, index) => { row.hidden = !expanded && index >= 15; });
    button.setAttribute('aria-expanded', String(expanded));
    button.textContent = expanded ? '접기' : `더 보기(전체 ${rows.length}건)`;
  }
  button.hidden = rows.length <= 15;
  button.onclick = () => show(button.getAttribute('aria-expanded') !== 'true');
  show(false);
}
