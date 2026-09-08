<!-- include start from serial/service/login.xml.i -->
<node name="login">
  <properties>
    <help>Login service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/idle-timeout.xml.i>
    #include <include/serial/service/utils/modem-in-out.xml.i>
    #include <include/serial/service/utils/banner.xml.i>
    #include <include/serial/service/utils/motd.xml.i>
    #include <include/serial/service/utils/session-timeout.xml.i>
    #include <include/serial/service/utils/transmit-string-no-end.xml.i>
  </children>
</node>
<!-- include end -->
