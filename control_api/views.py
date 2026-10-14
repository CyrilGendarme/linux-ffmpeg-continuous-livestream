from django.http import JsonResponse


def target_time(request):
    return JsonResponse({"detail": "not implemented"}, status=501)


def push_json(request):
    return JsonResponse({"detail": "not implemented"}, status=501)
