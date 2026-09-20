<?php
/** @var \OCP\IL10N $l */
\OCP\Util::addScript('talk_sip_bridge', 'admin-settings');
\OCP\Util::addStyle('talk_sip_bridge', 'admin-settings');
?>
<div id="talk_sip_bridge-admin" class="section">
	<h2><?php p($l->t('Talk SIP Bridge')); ?></h2>
	<p class="settings-hint">
		<?php p($l->t('Connects a SIP phone line to Talk calls. Incoming calls appear as regular participants; outgoing calls use Talk\'s own "call a phone number".')); ?>
	</p>

	<div id="talk_sip_bridge-status-block">
		<p>
			<strong><?php p($l->t('Status:')); ?></strong>
			<span id="talk_sip_bridge-status-text"><?php p($l->t('Loading …')); ?></span>
		</p>
		<button id="talk_sip_bridge-toggle-btn" class="button" disabled>
			<?php p($l->t('…')); ?>
		</button>
	</div>
</div>
