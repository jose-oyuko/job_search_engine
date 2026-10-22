"""factory-boy factories. Add one per model as models are introduced."""

import datetime

import factory
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.accounts.models import Invite, WaitlistEntry
from apps.jobs.models import Job
from apps.profiles.models import (
    Certification,
    Education,
    Experience,
    ExperienceBullet,
    Project,
    Skill,
    Track,
)
from apps.sources.models import Source


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


class InviteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Invite

    expires_at = factory.LazyFunction(lambda: timezone.now() + datetime.timedelta(days=7))


class WaitlistEntryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WaitlistEntry

    name = factory.Faker("name")
    email = factory.Sequence(lambda n: f"applicant{n}@example.com")
    reason = "Looking for remote work."


class SkillFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Skill

    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Skill {n}")


class TrackFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Track

    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Track {n}")
    target_titles = ["Support Engineer"]


class ExperienceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Experience

    user = factory.SubFactory(UserFactory)
    title = factory.Sequence(lambda n: f"Role {n}")
    company = "Acme Ltd"
    start_date = datetime.date(2021, 3, 1)


class BulletFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ExperienceBullet

    experience = factory.SubFactory(ExperienceFactory)
    user = factory.SelfAttribute("experience.user")
    text = factory.Sequence(lambda n: f"Did thing {n}")


class ProjectFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Project

    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Project {n}")


class EducationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Education

    user = factory.SubFactory(UserFactory)
    institution = factory.Sequence(lambda n: f"University {n}")


class CertificationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Certification

    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Cert {n}")


class SourceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Source

    name = factory.Sequence(lambda n: f"source-{n}")
    display_name = factory.Sequence(lambda n: f"Source {n}")
    kind = Source.Kind.JSON_API
    config = factory.LazyFunction(dict)


class JobFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Job

    source = factory.SubFactory(SourceFactory)
    external_id = factory.Sequence(lambda n: f"job-{n}")
    title = factory.Sequence(lambda n: f"Job title {n}")
    company = factory.Sequence(lambda n: f"Company {n}")
    first_seen_at = factory.LazyFunction(timezone.now)
    last_seen_at = factory.LazyFunction(timezone.now)
