from django.db import migrations, models
import django.db.models.deletion


def backfill_guest_profiles(apps, schema_editor):
    Guest = apps.get_model("booking", "Guest")
    GuestProfile = apps.get_model("booking", "GuestProfile")

    def normalize_phone(value):
        return "".join(character for character in str(value or "") if character.isdigit())

    for guest in Guest.objects.select_related("booking").order_by("id").iterator():
        phone = str(guest.phone or "").strip()
        email = str(guest.email or "").strip()
        normalized_phone = normalize_phone(phone)
        normalized_email = email.lower()
        candidates = GuestProfile.objects.filter(hotel_id=guest.booking.hotel_id)
        profile = None
        possible_duplicates = GuestProfile.objects.none()
        if normalized_phone:
            possible_duplicates = candidates.filter(normalized_phone=normalized_phone)
            if normalized_email:
                exact_email = possible_duplicates.filter(normalized_email=normalized_email)
                blank_email = possible_duplicates.filter(normalized_email="")
                if exact_email.count() == 1:
                    profile = exact_email.first()
                elif exact_email.count() == 0 and blank_email.count() == 1 and possible_duplicates.count() == 1:
                    profile = blank_email.first()
            elif possible_duplicates.count() == 1:
                profile = possible_duplicates.first()
        elif normalized_email:
            possible_duplicates = candidates.filter(normalized_email=normalized_email)
            if possible_duplicates.count() == 1:
                profile = possible_duplicates.first()
        if profile is None:
            is_duplicate = possible_duplicates.exists()
            if is_duplicate:
                possible_duplicates.update(possible_duplicate=True)
            profile = GuestProfile.objects.create(
                hotel_id=guest.booking.hotel_id,
                name=guest.name,
                phone=phone,
                normalized_phone=normalized_phone,
                email=email,
                normalized_email=normalized_email,
                possible_duplicate=is_duplicate,
            )
        elif normalized_email and not profile.normalized_email:
            profile.email = email
            profile.normalized_email = normalized_email
            profile.save(update_fields=["email", "normalized_email", "updated_at"])
        Guest.objects.filter(id=guest.id).update(profile_id=profile.id)


class Migration(migrations.Migration):
    dependencies = [("booking", "0040_bookingroom_meal_plan_snapshots")]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="booked_by_core_user_id",
            field=models.PositiveBigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.CreateModel(
            name="GuestProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("core_user_id", models.PositiveBigIntegerField(blank=True, db_index=True, null=True)),
                ("name", models.CharField(max_length=255)),
                ("phone", models.CharField(blank=True, max_length=64)),
                ("normalized_phone", models.CharField(blank=True, db_index=True, max_length=64)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("normalized_email", models.EmailField(blank=True, db_index=True, max_length=254)),
                ("possible_duplicate", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("hotel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="guest_profiles", to="booking.hotel")),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.AddField(
            model_name="guest",
            name="profile",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="stay_guests", to="booking.guestprofile"),
        ),
        migrations.RunPython(backfill_guest_profiles, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="guestprofile",
            index=models.Index(fields=["hotel", "normalized_phone"], name="booking_gue_hotel_i_82d85c_idx"),
        ),
        migrations.AddIndex(
            model_name="guestprofile",
            index=models.Index(fields=["hotel", "normalized_email"], name="booking_gue_hotel_i_42ae13_idx"),
        ),
    ]
