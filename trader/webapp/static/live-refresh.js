(function () {
  'use strict';

  function regionFromEvent(event) {
    var element = event.detail && event.detail.elt;
    return element && element.matches && element.matches('[data-live-region]')
      ? element
      : null;
  }

  function statusFor(region) {
    return document.querySelector('[data-live-status-for="' + region.id + '"]');
  }

  function setStatus(region, state, message) {
    var status = statusFor(region);
    if (!status) return;
    status.classList.remove('updating', 'paused', 'error');
    if (state) status.classList.add(state);
    var label = status.querySelector('span:last-child');
    if (label) label.textContent = message;
  }

  function currentTime() {
    return new Intl.DateTimeFormat('ru-RU', {
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    }).format(new Date());
  }

  function editable(element) {
    return Boolean(element && element.matches && element.matches(
      'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select, [contenteditable="true"]'
    ));
  }

  function editInProgress() {
    return Boolean(
      document.querySelector('form[data-dirty="true"], [data-live-region] details[open] form') ||
      editable(document.activeElement)
    );
  }

  function setAllStatuses(state, message) {
    document.querySelectorAll('[data-live-region]').forEach(function (region) {
      setStatus(region, state, message);
    });
  }

  document.addEventListener('input', function (event) {
    if (!editable(event.target)) return;
    var form = event.target.closest && event.target.closest('form');
    if (!form) return;
    form.dataset.dirty = 'true';
    setAllStatuses('paused', 'Автообновление на паузе: закончите редактирование');
  });

  document.addEventListener('change', function (event) {
    if (editable(event.target) && event.target.form) {
      event.target.form.dataset.dirty = 'true';
      setAllStatuses('paused', 'Автообновление на паузе: закончите редактирование');
    }
    if (!event.target.matches || !event.target.matches('[data-auto-submit]')) return;
    if (event.target.form) event.target.form.requestSubmit();
  });

  document.addEventListener('focusin', function (event) {
    if (editable(event.target)) {
      setAllStatuses('paused', 'Автообновление на паузе: поле ввода активно');
    }
  });

  document.addEventListener('focusout', function () {
    window.setTimeout(function () {
      if (!editInProgress()) setAllStatuses('', 'Автообновление возобновлено');
    }, 0);
  });

  document.addEventListener('submit', function (event) {
    if (event.target && event.target.matches && event.target.matches('form')) {
      delete event.target.dataset.dirty;
    }
  });

  document.addEventListener('toggle', function (event) {
    if (!event.target.matches || !event.target.matches('[data-live-region] details')) return;
    var region = event.target.closest('[data-live-region]');
    if (event.target.open) {
      setStatus(region, 'paused', 'Автообновление на паузе: открыта форма');
    } else {
      var form = event.target.querySelector('form');
      if (form) delete form.dataset.dirty;
      setStatus(region, '', 'Автообновление возобновлено');
    }
  }, true);

  document.body.addEventListener('htmx:beforeRequest', function (event) {
    var region = regionFromEvent(event);
    if (!region) return;
    if (document.hidden) {
      event.preventDefault();
      return;
    }
    if (editInProgress()) {
      event.preventDefault();
      setAllStatuses('paused', 'Автообновление на паузе: закончите редактирование');
      return;
    }
    setStatus(region, 'updating', 'Обновляем…');
  });

  document.body.addEventListener('htmx:afterRequest', function (event) {
    var region = regionFromEvent(event);
    if (!region) return;
    if (event.detail.successful) {
      setStatus(region, '', 'Обновлено ' + currentTime());
    } else {
      setStatus(region, 'error', 'Не удалось обновить; повторим автоматически');
    }
  });
}());
