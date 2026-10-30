import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit


@toolkit.blanket.actions
@toolkit.blanket.auth_functions
@toolkit.blanket.blueprints
@toolkit.blanket.cli
@toolkit.blanket.config_declarations
@toolkit.blanket.helpers
@toolkit.blanket.validators
class CronPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, "templates")
