<?php

declare(strict_types=1);

namespace OCA\FritzboxBridge\Settings;

use OCP\AppFramework\Http\TemplateResponse;
use OCP\Settings\ISettings;

class AdminSettings implements ISettings {
	public function getForm(): TemplateResponse {
		return new TemplateResponse('fritzboxbridge', 'admin', [], '');
	}

	public function getSection(): string {
		return 'fritzboxbridge';
	}

	public function getPriority(): int {
		return 10;
	}
}
