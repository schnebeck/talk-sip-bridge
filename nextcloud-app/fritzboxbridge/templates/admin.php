<?php
/** @var \OCP\IL10N $l */
\OCP\Util::addScript('fritzboxbridge', 'admin-settings');
\OCP\Util::addStyle('fritzboxbridge', 'admin-settings');
?>
<div id="fritzboxbridge-admin" class="section">
	<h2><?php p($l->t('FritzBox Talk Bridge')); ?></h2>
	<p class="settings-hint">
		<?php p($l->t('Connects a FritzBox phone line to Talk calls. Incoming calls appear as regular participants; outgoing calls use Talk\'s own "call a phone number".')); ?>
	</p>

	<div id="fritzboxbridge-status-block">
		<p>
			<strong><?php p($l->t('Status:')); ?></strong>
			<span id="fritzboxbridge-status-text"><?php p($l->t('Loading …')); ?></span>
		</p>
		<button id="fritzboxbridge-toggle-btn" class="button" disabled>
			<?php p($l->t('…')); ?>
		</button>
	</div>
</div>
