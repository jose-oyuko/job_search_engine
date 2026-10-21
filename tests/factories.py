"""factory-boy factories. Add one per model as models are introduced."""

import factory
from django.contrib.auth import get_user_model


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    full_name = factory.Faker("name")
    password = factory.PostGenerationMethodCall("set_password", "pass-12345-test")
    is_active = True

    @factory.post_generation
    def _save_password(self, create: bool, extracted: object, **kwargs: object) -> None:
        if create:
            self.save()
