<?php

declare(strict_types=1);

namespace OCA\TalkSipBridge\Settings;

use OCP\AppFramework\Http\TemplateResponse;
use OCP\Settings\ISettings;

class AdminSettings implements ISettings {
	public function getForm(): TemplateResponse {
		return new TemplateResponse('talk_sip_bridge', 'admin', [], '');
	}

	public function getSection(): string {
		return 'talk_sip_bridge';
	}

	public function getPriority(): int {
		return 10;
	}
}
