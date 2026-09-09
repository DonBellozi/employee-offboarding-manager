/* Часы сервера в шапке.
 *
 * Показывается именно серверное время: по нему считаются расписания воркеров
 * и контрольное окно кадровых выгрузок после 19:00. Время браузера здесь
 * бесполезно и вводило бы в заблуждение, если рабочее место в другом поясе.
 *
 * Стартовое значение приходит с сервера при рендере страницы, дальше секунды
 * докручиваются локально: постоянный опрос сервера ради часов не нужен.
 */
(() => {
  const node = document.getElementById('server-clock');
  if (!node) return;

  const dateValue = node.querySelector('[data-server-clock-date]');
  const timeValue = node.querySelector('[data-server-clock-time]');
  if (!dateValue || !timeValue) return;

  const epochMs = Number(node.dataset.epochMs);
  const offsetMinutes = Number(node.dataset.offsetMinutes);
  if (!Number.isFinite(epochMs) || !Number.isFinite(offsetMinutes)) return;

  // Смещение между часами браузера и сервера фиксируем один раз: дальше
  // локальный дрейф не накапливается, даже если вкладка была усыплена.
  const skewMs = epochMs - Date.now();
  const pad = (input) => String(input).padStart(2, '0');

  function render() {
    // Сдвигаем момент на смещение сервера и читаем его UTC-компоненты:
    // получаем настенное время сервера независимо от пояса браузера.
    const shifted = new Date(Date.now() + skewMs + offsetMinutes * 60000);
    dateValue.textContent =
      `${pad(shifted.getUTCDate())}.${pad(shifted.getUTCMonth() + 1)}.` +
      `${shifted.getUTCFullYear()}`;
    timeValue.textContent =
      `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:` +
      `${pad(shifted.getUTCSeconds())}`;
  }

  render();
  // Тик выравнивается по границе секунды, чтобы цифры не «дрожали».
  window.setTimeout(() => {
    render();
    window.setInterval(render, 1000);
  }, 1000 - ((Date.now() + skewMs) % 1000));
})();
