document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('compareForm');
  const btn = document.getElementById('submitBtn');
  const spin = document.getElementById('submitSpinner');

  if (form && btn && spin) {
    form.addEventListener('submit', () => {
      spin.classList.remove('d-none');
      btn.setAttribute('disabled', 'true');
    });
  }

  // Show chosen filenames under file inputs
  document.querySelectorAll('input[type="file"]').forEach(input => {
    input.addEventListener('change', () => {
      const hint = input.parentElement.querySelector('.sr-file-hint');
      if (hint && input.files && input.files[0]) {
        hint.textContent = input.files[0].name;
      }
    });
  });
});
