from django.contrib import admin

from booking.models import AddOn, Booking, DailyInventory, DailyRate, Hotel, Payment, PhysicalRoom, RatePlan, RatePeriod, RoomType


admin.site.register([Hotel, RoomType, PhysicalRoom, RatePlan, RatePeriod, DailyInventory, DailyRate, AddOn, Booking, Payment])
