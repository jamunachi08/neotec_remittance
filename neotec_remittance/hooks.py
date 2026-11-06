app_name = "neotec_remittance"
app_title = "Neotec Remittance"
app_publisher = "Neotec Integrated Solutions"
app_description = "Remittance instruction engine, append-only subledger and bank execution for Frappe/ERPNext"
app_email = "info@neotec.ai"
app_license = "Proprietary"
app_version = "0.1.1"

required_apps = ["erpnext"]

after_install = "neotec_remittance.install.after_install"
after_migrate = "neotec_remittance.install.after_migrate"

scheduler_events = {
	"cron": {
		"* * * * *": [
			"neotec_remittance.tasks.every_minute",
		],
		"*/2 * * * *": [
			"neotec_remittance.services.execution.release_due",
		],
		"*/5 * * * *": [
			"neotec_remittance.tasks.every_five_minutes",
		],
		"*/15 * * * *": [
			"neotec_remittance.services.funding.expire_unfunded",
		],
	},
	"hourly_long": [
		"neotec_remittance.services.safeguarding.run_checks",
	],
	"daily_long": [
		"neotec_remittance.tasks.daily",
	],
}

# Pluggable integration points (other apps may extend these lists):
# neotec_remittance_key_provider = "my_app.kms.KmsProvider"            # envelope-encryption master key (KMS/HSM)
# neotec_remittance_screening_providers = ["my_app.screening.screen"]  # external sanctions screening
# neotec_remittance_verify_authorization = ["my_app.auth.verify"]      # step-up authorization verification
