<!-- include start from serial/service/utils/modem-in-out.xml.i -->
<node name="modem">
  <properties>
    <help>Modem setting</help>
  </properties>
  <children>
    <leafNode name="initialization-string">
      <properties>
        <help>A series of commands sent to the modem by a communications program at start up</help>
        <constraint>
          <regex>.{0,61}</regex>
        </constraint>
        <constraintErrorMessage>Initialization string too long (limit 61 characters)</constraintErrorMessage>
      </properties>
    </leafNode>
    <node name="dial">
      <properties>
        <help>Dial setting</help>
      </properties>
      <children>
        <leafNode name="in">
          <properties>
            <help>Enable this when serial port is remote and will be dialing in via modem or ISDN TA</help>
            <valueless/>
          </properties>
        </leafNode>
        <node name="out">
          <properties>
            <help>Enable this when the modem should dial out to a remote device</help>
          </properties>
          <children>
            #include <include/serial/service/utils/modem-dial-outbound-config.xml.i>
          </children>
        </node>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
