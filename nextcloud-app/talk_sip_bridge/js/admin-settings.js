(function() {
	'use strict';

	const base = OC.generateUrl('/apps/talk_sip_bridge');
	const statusText = document.getElementById('talk_sip_bridge-status-text');
	const toggleBtn = document.getElementById('talk_sip_bridge-toggle-btn');

	function renderStatus(data) {
		if (data && data.error) {
			statusText.textContent = t('talk_sip_bridge', 'Error: {message}', {message: data.error});
			toggleBtn.disabled = true;
			toggleBtn.textContent = '…';
			return;
		}
		const on = !!(data && data.registered);
		statusText.textContent = on
			? t('talk_sip_bridge', 'Active (registered as {username})', {username: data.username || '?'})
			: t('talk_sip_bridge', 'Inactive (not registered)');
		toggleBtn.disabled = false;
		toggleBtn.textContent = on ? t('talk_sip_bridge', 'Turn off') : t('talk_sip_bridge', 'Turn on');
	}

	function fetchStatus() {
		fetch(base + '/status', {headers: {requesttoken: OC.requestToken}})
			.then((r) => r.json())
			.then(renderStatus)
			.catch((err) => {
				statusText.textContent = t('talk_sip_bridge', 'Error loading status: {message}', {message: String(err)});
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
				statusText.textContent = t('talk_sip_bridge', 'Error: {message}', {message: String(err)});
				toggleBtn.disabled = false;
			});
	});

	fetchStatus();
	setInterval(fetchStatus, 5000);
})();
