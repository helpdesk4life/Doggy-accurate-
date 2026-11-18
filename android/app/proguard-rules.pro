# MapLibre keeps its native map objects reachable through JNI.
-keep class org.maplibre.android.** { *; }
-dontwarn org.maplibre.android.**

# kotlinx.serialization generated serializers.
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-keepclassmembers class com.mikywalk.** {
    *** Companion;
}
-keepclasseswithmembers class com.mikywalk.** {
    kotlinx.serialization.KSerializer serializer(...);
}
