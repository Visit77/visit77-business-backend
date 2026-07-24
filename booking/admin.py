from django.contrib import admin

from booking.models import AddOn, Booking, DailyInventory, DailyRate, Hotel, MealPlan, Payment, PhysicalRoom, RatePlan, RatePeriod, RoomType, RoomTypeMealPlan


admin.site.register([Hotel, RoomType, MealPlan, RoomTypeMealPlan, PhysicalRoom, RatePlan, RatePeriod, DailyInventory, DailyRate, AddOn, Booking, Payment])
