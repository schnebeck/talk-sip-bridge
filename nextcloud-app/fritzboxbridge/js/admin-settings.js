(function() {
	'use strict';

	const base = OC.generateUrl('/apps/fritzboxbridge');
	const statusText = document.getElementById('fritzboxbridge-status-text');
	const toggleBtn = document.getElementById('fritzboxbridge-toggle-btn');

	function renderStatus(data) {
		if (data && data.error) {
			statusText.textContent = t('fritzboxbridge', 'Error: {message}', {message: data.error});
			toggleBtn.disabled = true;
			toggleBtn.textContent = '…';
			return;
		}
		const on = !!(data && data.registered);
		statusText.textContent = on
			? t('fritzboxbridge', 'Active (registered as {username})', {username: data.username || '?'})
			: t('fritzboxbridge', 'Inactive (not registered)');
		toggleBtn.disabled = false;
		toggleBtn.textContent = on ? t('fritzboxbridge', 'Turn off') : t('fritzboxbridge', 'Turn on');
	}

	function fetchStatus() {
		fetch(base + '/status', {headers: {requesttoken: OC.requestToken}})
			.then((r) => r.json())
			.then(renderStatus)
			.catch((err) => {
				statusText.textContent = t('fritzboxbridge', 'Error loading status: {message}', {message: String(err)});
			});
	}

	toggleBtn.addEventListener('click', function() {
		toggleBtn.disabled = true;
		fetch(base + '/toggle', {
			method: 'POST',
			headers: {requesttoken: OC.requestToken, 'Content-Type': 'application/json'},
		})
			.then((r) => r.json())
			.then((data) => {
				renderStatus(data);
				fetchStatus();
			})
			.catch((err) => {
				statusText.textContent = t('fritzboxbridge', 'Error: {message}', {message: String(err)});
				toggleBtn.disabled = false;
			});
	});

	fetchStatus();
	setInterval(fetchStatus, 5000);
})();
